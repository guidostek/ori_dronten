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
from google.cloud.firestore_v1.base_query import FieldFilter # Nieuw voor de warning
from config import *
from firebase_client import db
from notification_service import send_push_notification

# --- CONFIGURATIE FALLBACKS ---
DOC_DETAIL_DELAY = globals().get('DOC_DETAIL_DELAY', 0.1) 
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
    return result if isinstance(result, list) else []

# --- MEDIA SCRAPER (Verbeterd voor schone titels zoals 'J.N. Ammerlaan') ---

def extract_media_enriched(relative_url, session, items_lijst):
    """
    Scrapet de publieke vergaderpagina voor volledige media en fragmenten.
    Gebruikt de tekst van de link op de pagina voor duidelijke titels.
    """
    if not relative_url:
        return [], items_lijst, None
        
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    full_audio_url = None
    
    try:
        response = session.get(full_url, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')

            # 1. Zoek HOOFD opnames (Audio & Video)
            for a in soup.find_all('a', href=re.compile(r'\.(mp3|mp4)$', re.I)):
                href = a.get('href')
                url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
                m_type = 'audio' if url.lower().endswith('.mp3') else 'video'
                
                if m_type == 'audio' and not full_audio_url:
                    full_audio_url = url

                if not any(m['url'] == url for m in meeting_media):
                    meeting_media.append({
                        'title': a.get_text(strip=True) or "Volledige opname",
                        'url': url,
                        'type': m_type
                    })

            # Zoek naar video iframes (YouTube/Notubiz)
            for iframe in soup.find_all('iframe'):
                src = iframe.get('src')
                if src and any(p in src for p in ['notubiz', 'youtube', 'vimeo', 'raadsinformatie']):
                    v_url = src if src.startswith('http') else f"https:{src}"
                    if not any(m['url'] == v_url for m in meeting_media):
                        meeting_media.append({
                            'title': 'Videoverslag / Stream',
                            'url': v_url,
                            'type': 'video'
                        })

            # 2. Zoek fragmenten per agendapunt
            for item in items_lijst:
                title = item.get('title', '')
                if not title: continue
                
                container = soup.find(lambda tag: tag.name in ['div', 'li', 'tr'] and title in tag.get_text())
                
                item_media = []
                if container:
                    for link in container.find_all(['a', 'button'], href=True):
                        href = link.get('href')
                        if any(ext in href.lower() for ext in ['.mp3', '.mp4', '#t=', 'start=']):
                            l_url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
                            
                            # CHIRURGISCH: Gebruik de tekst van de link (bijv. de naam van de spreker)
                            link_label = link.get_text(strip=True)
                            
                            # Als er een label is (zoals 'J.N. Ammerlaan'), gebruik die direct.
                            # We plakken het agendapunt er niet meer voor om de lijst schoon te houden.
                            display_title = link_label if link_label else f"Fragment: {title}"
                            
                            item_media.append({
                                'title': display_title,
                                'url': l_url,
                                'type': 'audio' if '.mp3' in l_url.lower() else 'video'
                            })
                item['item_media'] = item_media

    except Exception as e:
        logging.warning(f"[-] Scraper fout bij {full_url}: {e}")
        
    return meeting_media, items_lijst, full_audio_url

# --- HOOFD SYNC LOGICA ---

def run_deep_sync():
    date_from_str = get_sync_start_date_str()
    logging.info(f"Start Deep Sync (SCHEMA V2) vanaf datum: {date_from_str}")

    notified_meetings = load_cache(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_cache(NOTIFIED_DOCS_FILE)
    
    new_meetings_found = False
    new_docs_found = False

    session = requests.Session()
    session.headers.update({'User-Agent': 'DrontenRaadApp-Monitor/2.2'})

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
        if resp.status_code != 200:
            logging.error(f"Fout bij ophalen meetings: {resp.status_code}")
            return
            
        meetings = extract_api_data(resp.json(), endpoint_naam=meetings_url)

        for meeting in meetings:
            m_id = str(meeting.get('id'))
            if not m_id or m_id == 'None': continue
            
            processed_meeting_ids.add(int(m_id))
            m_date = meeting.get('date', '')
            if m_date < date_from_str: continue

            meeting_ref = db.collection('vergaderingen').document(m_id)
            meeting_snap = meeting_ref.get()
            existing_m = meeting_snap.to_dict() if meeting_snap.exists else {}

            # Haal agendapunten op
            updated_items_list = []
            items_url = f"{DRONTEN_API_V2}/meetings/{m_id}/meetingitems"
            items_resp = session.get(items_url, timeout=TIMEOUT_SECONDS)
            
            if items_resp.status_code == 200:
                items = extract_api_data(items_resp.json(), endpoint_naam=items_url)
                for item in items:
                    item_id = str(item.get('id'))
                    item_id_int = int(item_id)
                    api_item_ids.add(item_id_int)
                    item_title = item.get('title') or item.get('description') or f"Punt {item_id}"
                    
                    # Documenten per item
                    nested_docs_list = []
                    docs_url = f"{DRONTEN_API_V2}/meetingitems/{item_id}/documents"
                    docs_resp = session.get(docs_url, timeout=TIMEOUT_SECONDS)
                    
                    if docs_resp.status_code == 200:
                        docs = extract_api_data(docs_resp.json())
                        for doc in docs:
                            d_id = str(doc['id'])
                            api_doc_ids.add(int(d_id))
                            
                            # Gedetailleerde metadata fetch
                            doc_info = doc
                            if DOC_DETAIL_DELAY > 0:
                                time.sleep(DOC_DETAIL_DELAY)
                            try:
                                d_detail = session.get(f"{DRONTEN_API_V2}/documents/{d_id}", timeout=TIMEOUT_SECONDS)
                                if d_detail.status_code == 200:
                                    doc_info = extract_api_data(d_detail.json())
                            except:
                                pass

                            doc_data = {
                                'id': int(d_id),
                                'title': doc_info.get('description') or doc_info.get('fileName') or "Document",
                                'confidential': bool(doc_info.get('confidential', 0)),
                                'url': f"{DRONTEN_API_V2}/documents/{d_id}/download",
                                'meeting_id': int(m_id),
                                'item_id': item_id_int
                            }
                            nested_docs_list.append(doc_data)
                            
                            # Schrijf naar raadstukken collectie
                            batch.set(db.collection('raadstukken').document(d_id), {
                                **doc_data, 
                                'last_sync': firestore.SERVER_TIMESTAMP
                            }, merge=True)
                            batch_count += 1
                            commit_batch_if_needed()

                    # Platte agendapunten collectie voor zoekopdrachten
                    batch.set(db.collection('agendapunten').document(item_id), {
                        'id': item_id_int,
                        'meeting_id': int(m_id),
                        'title': item_title,
                        'last_sync': firestore.SERVER_TIMESTAMP
                    }, merge=True)
                    batch_count += 1

                    updated_items_list.append({
                        'id': item_id_int,
                        'title': item_title,
                        'number': str(item.get('number', '')),
                        'sortorder': item.get('sortOrder', 0),
                        'description': item.get('description', ''),
                        'documents': nested_docs_list
                    })

            # --- MEDIA SCRAPER ---
            m_media, enriched_items, full_audio = extract_media_enriched(meeting.get('url'), session, updated_items_list)

            new_meeting_data = {
                'id': int(m_id),
                'schema_version': 2,
                'title': clean_html(meeting.get('description', '')) or meeting.get('title') or "Vergadering",
                'date': m_date,
                'confidential': bool(meeting.get('confidential', 0)),
                'full_audio_url': full_audio, # <--- Voor de player bovenin
                'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                'location': meeting.get('location', ''),
                'items': enriched_items,
                'media_attachments': m_media,
                'last_sync': firestore.SERVER_TIMESTAMP
            }

            # Bepaal of update nodig is
            needs_update = not meeting_snap.exists
            if not needs_update:
                for k, v in new_meeting_data.items():
                    if str(existing_m.get(k)) != str(v):
                        needs_update = True
                        break

            if needs_update:
                batch.set(meeting_ref, new_meeting_data, merge=True)
                batch_count += 1
                commit_batch_if_needed()
                
                if m_id not in notified_meetings:
                    send_push_notification(f"Nieuwe agenda: {new_meeting_data['title']}", f"Datum: {m_date[:10]}")
                    notified_meetings.add(m_id)
                    new_meetings_found = True

        # --- DEEL 2: GLOBALE RAADSTUKKEN SYNC ---
        g_resp = session.get(f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50", timeout=TIMEOUT_SECONDS)
        if g_resp.status_code == 200:
            for d in extract_api_data(g_resp.json()):
                d_id = str(d['id'])
                if d_id not in notified_docs:
                    title = d.get('description') or d.get('fileName') or f"Stuk {d_id}"
                    db.collection('raadstukken').document(d_id).set({
                        'id': int(d_id),
                        'schema_version': 2,
                        'title': title,
                        'url': f"{DRONTEN_API_V2}/documents/{d_id}/download",
                        'last_sync': firestore.SERVER_TIMESTAMP
                    }, merge=True)
                    send_push_notification("Nieuw raadstuk gepubliceerd", title)
                    notified_docs.add(d_id)
                    new_docs_found = True

        # --- DEEL 3: CLEANUP (Gecorrigeerd voor warning) ---
        if processed_meeting_ids:
            for mid in processed_meeting_ids:
                for col, valid_ids in [('agendapunten', api_item_ids), ('raadstukken', api_doc_ids)]:
                    query = db.collection(col).where(filter=FieldFilter('meeting_id', '==', mid)).stream()
                    for doc in query:
                        if int(doc.id) not in valid_ids:
                            batch.delete(doc.reference)
                            batch_count += 1
                commit_batch_if_needed()

        commit_batch_if_needed(force=True)

    except Exception as e:
        logging.error(f"Fout in run_deep_sync: {e}")

    if new_meetings_found: save_cache(NOTIFIED_MEETINGS_FILE, notified_meetings)
    if new_docs_found: save_cache(NOTIFIED_DOCS_FILE, notified_docs)

if __name__ == "__main__":
    run_deep_sync()