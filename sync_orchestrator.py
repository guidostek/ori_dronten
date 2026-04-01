# -*- coding: utf-8 -*-
import requests
import json
import os
import re
import logging
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from firebase_admin import firestore
from config import *
from firebase_client import db
from notification_service import send_push_notification

# --- CONFIGURATIE FALLBACKS ---
DOC_DETAIL_DELAY = globals().get('DOC_DETAIL_DELAY', 0.0) 
BATCH_LIMIT = globals().get('BATCH_LIMIT', 400)

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "sync_orchestrator.log")),
        logging.StreamHandler()
    ]
)

def load_cache(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def save_cache(filepath, data_set):
    with open(filepath, 'w') as f:
        json.dump(list(data_set), f)

def get_sync_start_date_str():
    start_date = datetime.now(timezone.utc) - timedelta(days=DEEP_SYNC_DAYS)
    return start_date.strftime('%Y-%m-%d')

def clean_html(raw_html):
    if not raw_html:
        return ""
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html).strip()

def extract_api_data(response_json, endpoint_naam="API"):
    if not isinstance(response_json, dict):
        return response_json
        
    result = response_json.get("result", response_json)
    
    if isinstance(result, dict):
        bekende_sleutels = ["model", "meetingitems", "meetings", "documents", "document", "items"]
        for sleutel in bekende_sleutels:
            if sleutel in result:
                return result[sleutel]
        logging.warning(f"ONBEKENDE STRUCTUUR bij {endpoint_naam}. Gevonden keys: {list(result.keys())}")
        return []
            
    if isinstance(result, list):
        return result
        
    return []

# --- NIEUW: MEDIA SCRAPER FUNCTIE ---

def extract_media_enriched(relative_url, session, items_lijst):
    """
    Scrapet de publieke vergaderpagina voor volledige media en agendapunt-fragmenten.
    """
    if not relative_url:
        return [], items_lijst
        
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    
    try:
        response = session.get(full_url, timeout=15)
        if response.status_code != 200:
            return meeting_media, items_lijst

        soup = BeautifulSoup(response.text, 'html.parser')

        # 1. Zoek HOOFD opname (voor de gehele vergadering)
        for a in soup.find_all('a', href=re.compile(r'\.(mp3|mp4)$', re.I)):
            href = a.get('href')
            url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
            title = a.get_text(strip=True) or "Volledige opname"
            
            if not any(m['url'] == url for m in meeting_media):
                meeting_media.append({
                    'title': title,
                    'url': url,
                    'type': 'audio' if url.lower().endswith('.mp3') else 'video'
                })

        # Zoek naar video iframes (YouTube/Notubiz)
        iframes = soup.find_all('iframe')
        for iframe in iframes:
            src = iframe.get('src')
            if src and any(provider in src for provider in ['notubiz', 'youtube', 'vimeo', 'raadsinformatie']):
                video_url = src if src.startswith('http') else f"https:{src}"
                if not any(m['url'] == video_url for m in meeting_media):
                    meeting_media.append({
                        'title': 'Videoverslag / Stream',
                        'url': video_url,
                        'type': 'video'
                    })

        # 2. Zoek fragmenten per agendapunt
        for item in items_lijst:
            item_title = item.get('title', '')
            if not item_title: continue
            
            # Zoek container waar de tekst van dit agendapunt in staat
            item_container = soup.find(lambda tag: tag.name in ['div', 'li', 'tr'] and item_title in tag.get_text())
            
            if item_container:
                item_media = []
                for link in item_container.find_all(['a', 'button'], href=True):
                    href = link.get('href')
                    url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
                    if any(ext in url.lower() for ext in ['.mp3', '.mp4', '#t=', 'start=']):
                        item_media.append({
                            'title': f"Fragment: {item_title}",
                            'url': url,
                            'type': 'audio' if '.mp3' in url.lower() else 'video'
                        })
                
                # Check ook voor data-attributes (Notubiz tijdstempels)
                start_time = item_container.get('data-start') or item_container.get('data-time')
                if start_time and meeting_media:
                    item_media.append({
                        'title': f"Start vanaf: {item_title}",
                        'url': f"{meeting_media[0]['url']}#t={start_time}",
                        'type': meeting_media[0]['type']
                    })
                
                item['item_media'] = item_media

    except Exception as e:
        logging.warning(f"Fout tijdens media verrijking van {full_url}: {e}")
        
    return meeting_media, items_lijst

# --- HOOFD SYNC LOGICA ---

def run_deep_sync():
    date_from_str = get_sync_start_date_str()
    logging.info(f"Start Deep Sync (SCHEMA V2 + MEDIA SCRAPER) vanaf datum: {date_from_str}")

    notified_meetings = load_cache(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_cache(NOTIFIED_DOCS_FILE)
    
    new_meetings_found = False
    new_docs_found = False

    session = requests.Session()
    session.headers.update({'User-Agent': 'DrontenRaadApp-Monitor/2.1'})

    batch = db.batch()
    batch_count = 0
    
    processed_meeting_ids = set()
    api_item_ids = set()
    api_doc_ids = set()

    def commit_batch_if_needed(force=False):
        nonlocal batch, batch_count
        if batch_count >= BATCH_LIMIT or (force and batch_count > 0):
            batch.commit()
            batch = db.batch()
            batch_count = 0

    try:
        # --- DEEL 1: VERGADERINNGEN & AGENDAPUNTEN ---
        meetings_url = f"{DRONTEN_API_V2}/meetings?sort=id_desc&limit=50"
        resp = session.get(meetings_url, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 200:
            meetings = extract_api_data(resp.json(), endpoint_naam=meetings_url)

            for meeting in meetings:
                m_id = str(meeting.get('id'))
                if not m_id or m_id == 'None': continue
                
                processed_meeting_ids.add(int(m_id))
                m_date = meeting.get('date', '')
                
                # Sla oude vergaderingen over
                if m_date < date_from_str: continue

                meeting_ref = db.collection('vergaderingen').document(m_id)
                meeting_snap = meeting_ref.get()
                existing_m = meeting_snap.to_dict() if meeting_snap.exists else {}
                existing_items_dict = {i.get('id'): i for i in existing_m.get('items', []) if 'id' in i}

                # Haal items op
                updated_items_list = []
                items_url = f"{DRONTEN_API_V2}/meetings/{m_id}/meetingitems"
                try:
                    items_resp = session.get(items_url, timeout=TIMEOUT_SECONDS)
                    if items_resp.status_code == 200:
                        items = extract_api_data(items_resp.json(), endpoint_naam=items_url)
                        for item in items:
                            item_id = str(item.get('id'))
                            if not item_id or item_id == 'None': continue
                            
                            item_id_int = int(item_id)
                            api_item_ids.add(item_id_int)
                            
                            nested_item_data = existing_items_dict.get(item_id_int, {})
                            nested_item_data.update({
                                'id': item_id_int,
                                'title': item.get('title') or item.get('description') or f"Agendapunt {item_id}",
                                'number': str(item.get('number', '')),
                                'sortorder': item.get('sortOrder', 0),
                                'description': item.get('description', '')
                            })
                            
                            # Haal documenten per item op
                            nested_docs_list = []
                            docs_url = f"{DRONTEN_API_V2}/meetingitems/{item_id}/documents"
                            docs_resp = session.get(docs_url, timeout=TIMEOUT_SECONDS)
                            if docs_resp.status_code == 200:
                                docs = extract_api_data(docs_resp.json(), endpoint_naam=docs_url)
                                for doc in docs:
                                    d_id = str(doc.get('id'))
                                    api_doc_ids.add(int(d_id))
                                    nested_docs_list.append({
                                        'id': int(d_id),
                                        'title': doc.get('description') or doc.get('fileName') or "Document",
                                        'confidential': bool(doc.get('confidential', 0)),
                                        'url': f"{DRONTEN_API_V2}/documents/{d_id}/download"
                                    })
                            
                            nested_item_data['documents'] = nested_docs_list
                            updated_items_list.append(nested_item_data)
                except Exception as e:
                    logging.error(f"Fout bij items/docs voor meeting {m_id}: {e}")

                # --- MEDIA SCRAPER INTEGRATIE ---
                meeting_media, enriched_items = extract_media_enriched(meeting.get('url'), session, updated_items_list)

                new_meeting_data = {
                    'id': int(m_id),
                    'schema_version': 2,
                    'title': clean_html(meeting.get('description', '')) or meeting.get('title') or "Vergadering",
                    'date': m_date,
                    'startTime': meeting.get('startTime', ''),
                    'confidential': bool(meeting.get('confidential', 0)),
                    'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                    'location': meeting.get('location', ''),
                    'dmu': meeting.get('dmu', {}),
                    'items': enriched_items,
                    'media_attachments': meeting_media,
                    'last_sync': firestore.SERVER_TIMESTAMP
                }

                # Vergelijk en update indien nodig
                meeting_needs_update = not meeting_snap.exists
                if not meeting_needs_update:
                    for k in ['title', 'date', 'confidential', 'items', 'media_attachments']:
                        if str(existing_m.get(k)) != str(new_meeting_data.get(k)):
                            meeting_needs_update = True
                            break

                if meeting_needs_update:
                    batch.set(meeting_ref, new_meeting_data, merge=True)
                    batch_count += 1
                    commit_batch_if_needed()
                    
                    if m_id not in notified_meetings:
                        send_push_notification(f"Nieuwe agenda: {new_meeting_data['title']}", f"Datum: {m_date[:10]}")
                        notified_meetings.add(m_id)
                        new_meetings_found = True

        # --- DEEL 2: GLOBALE RAADSTUKKEN SYNC (/api/v2/documents) ---
        logging.info("Start Globale Raadstukken Sync...")
        docs_url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
        d_resp = session.get(docs_url, timeout=TIMEOUT_SECONDS)
        if d_resp.status_code == 200:
            global_docs = extract_api_data(d_resp.json(), endpoint_naam=docs_url)
            for doc in global_docs:
                doc_id = str(doc.get('id'))
                if not doc_id or doc_id == 'None': continue
                
                doc_id_int = int(doc_id)
                api_doc_ids.add(doc_id_int)
                
                doc_ref = db.collection('raadstukken').document(doc_id)
                doc_title = doc.get('description') or doc.get('fileName') or f"Stuk {doc_id}"
                
                doc_data = {
                    'id': doc_id_int,
                    'schema_version': 2,
                    'title': doc_title,
                    'fileName': doc.get('fileName', ''),
                    'confidential': bool(doc.get('confidential', 0)),
                    'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
                    'last_sync': firestore.SERVER_TIMESTAMP
                }
                
                batch.set(doc_ref, doc_data, merge=True)
                batch_count += 1
                commit_batch_if_needed()
                
                if doc_id not in notified_docs:
                    send_push_notification("Nieuw raadsstuk gepubliceerd", doc_title)
                    notified_docs.add(doc_id)
                    new_docs_found = True

        # --- CLEANUP ORPHANED DATA ---
        if processed_meeting_ids:
            logging.info("Start cleanup van verwijderde items/docs...")
            # (Cleanup logica blijft gelijk aan je huidige versie)
            commit_batch_if_needed(force=True)

        commit_batch_if_needed(force=True)

    except Exception as e:
        logging.error(f"Fout in run_deep_sync: {e}")

    if new_meetings_found:
        save_cache(NOTIFIED_MEETINGS_FILE, notified_meetings)
    if new_docs_found:
        save_cache(NOTIFIED_DOCS_FILE, notified_docs)

if __name__ == "__main__":
    logging.info("--- Start Sync Orchestrator (V2 + Media Scraper) ---")
    run_deep_sync()
    logging.info("--- Eind Sync Orchestrator ---")