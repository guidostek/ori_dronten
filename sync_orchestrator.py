# -*- coding: utf-8 -*-
import requests
import json
import os
import re
import logging
import time
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore
from config import *
from firebase_client import db
from notification_service import send_push_notification

# --- CONFIGURATIE FALLBACKS ---
# Delay standaard op 0.0 gezet voor maximale snelheid. 
# Verhoog deze in je config.py naar bijv. 0.1 of 0.2 als de Dronten API een HTTP 429 (Too Many Requests) teruggeeft.
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

def run_deep_sync():
    date_from_str = get_sync_start_date_str()
    logging.info(f"Start Deep Sync (NESTED MODE & BATCHING) vanaf datum: {date_from_str}")

    notified_meetings = load_cache(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_cache(NOTIFIED_DOCS_FILE)
    
    new_meetings_found = False
    new_docs_found = False

    # --- HTTP SESSION VOOR SNELHEID ---
    # Hergebruikt TCP connecties en SSL handshakes, cruciaal voor duizenden requests!
    session = requests.Session()
    session.headers.update({'User-Agent': 'DrontenRaadApp-Monitor/2.1'})

    meetings_url = f"{DRONTEN_API_V2}/meetings?sort=id_desc&limit=50"
    
    # --- BATCH & CLEANUP TRACKING ---
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
        # Gebruik session.get() in plaats van requests.get()
        resp = session.get(meetings_url, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logging.error(f"API weigerde toegang tot meetings: HTTP {resp.status_code}")
            return

        meetings = extract_api_data(resp.json(), endpoint_naam=meetings_url)

        for meeting in meetings:
            m_id = str(meeting.get('id'))
            if not m_id or m_id == 'None':
                continue
            
            processed_meeting_ids.add(int(m_id))
            is_debug_target = (m_id in ['1467', '1468'])
                
            m_date = meeting.get('date', '')
            raw_desc = meeting.get('description', '')
            clean_desc = clean_html(raw_desc)
            dmu_name = meeting.get('dmu', {}).get('name', '')
            
            if clean_desc:
                title = clean_desc
            elif dmu_name:
                title = dmu_name
            else:
                title = meeting.get('title') or "Vergadering"
            
            if is_debug_target:
                logging.info(f"\n==================================================")
                logging.info(f"--- Start verwerking Meeting ID: {m_id} ({title}) ---")

            meeting_ref = db.collection('vergaderingen').document(m_id)
            meeting_snap = meeting_ref.get()
            
            existing_m = meeting_snap.to_dict() if meeting_snap.exists else {}
            existing_items_list = existing_m.get('items', [])
            existing_items_dict = {i.get('id'): i for i in existing_items_list if 'id' in i}

            new_meeting_data = {
                'id': int(m_id),
                'schema_version': 2,
                'title': title,
                'date': m_date,
                'startTime': meeting.get('startTime', ''),
                'confidential': bool(meeting.get('confidential', 0)),
                'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                'location': meeting.get('location', ''),
                'description': raw_desc,
                'dmu': meeting.get('dmu', {})
            }

            meeting_needs_update = False
            items_url = f"{DRONTEN_API_V2}/meetings/{m_id}/meetingitems"
            updated_items_list = []
            
            try:
                items_resp = session.get(items_url, timeout=TIMEOUT_SECONDS)
                if items_resp.status_code == 200:
                    items = extract_api_data(items_resp.json(), endpoint_naam=items_url)
                    
                    for item in items:
                        item_id = str(item.get('id'))
                        if not item_id or item_id == 'None':
                            continue
                        
                        item_id_int = int(item_id)
                        api_item_ids.add(item_id_int)
                        item_title = item.get('title') or item.get('description') or f"Agendapunt {item_id}"
                        
                        nested_item_data = existing_items_dict.get(item_id_int, {})
                        nested_item_data['id'] = item_id_int
                        nested_item_data['title'] = item_title
                        nested_item_data['number'] = str(item.get('number', ''))
                        nested_item_data['sortorder'] = item.get('sortOrder', 0)
                        nested_item_data['description'] = item.get('description', '')
                        
                        if 'item_media' not in nested_item_data: nested_item_data['item_media'] = []
                        if 'portfolioHolder' not in nested_item_data: nested_item_data['portfolioHolder'] = ''
                        if 'anchor' not in nested_item_data: nested_item_data['anchor'] = '#page'
                        
                        nested_docs_list = []
                        docs_url = f"{DRONTEN_API_V2}/meetingitems/{item_id}/documents"
                        
                        try:
                            docs_resp = session.get(docs_url, timeout=TIMEOUT_SECONDS)
                            if docs_resp.status_code == 200:
                                docs = extract_api_data(docs_resp.json(), endpoint_naam=docs_url)
                                
                                for doc in docs:
                                    doc_id = str(doc.get('id'))
                                    if not doc_id or doc_id == 'None':
                                        continue
                                    
                                    doc_id_int = int(doc_id)
                                    api_doc_ids.add(doc_id_int)
                                    
                                    doc_detail_url = f"{DRONTEN_API_V2}/documents/{doc_id}"
                                    doc_info = doc
                                    
                                    if DOC_DETAIL_DELAY > 0:
                                        time.sleep(DOC_DETAIL_DELAY)
                                        
                                    try:
                                        detail_resp = session.get(doc_detail_url, timeout=TIMEOUT_SECONDS)
                                        if detail_resp.status_code == 200:
                                            extracted_detail = extract_api_data(detail_resp.json(), endpoint_naam=doc_detail_url)
                                            if isinstance(extracted_detail, dict):
                                                doc_info = extracted_detail
                                    except Exception as e:
                                        logging.warning(f"Fout bij ophalen document details {doc_id}: {e}")

                                    doc_title = doc_info.get('description') or doc_info.get('fileName') or "Document"
                                    download_url = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
                                    
                                    new_doc_data = {
                                        'id': doc_id_int,
                                        'fileName': doc_title,
                                        'title': doc_title,
                                        'confidential': bool(doc_info.get('confidential', 0)),
                                        'url': download_url
                                    }
                                    if 'fileSize' in doc_info:
                                        new_doc_data['fileSize'] = doc_info['fileSize']
                                    if 'documentTypeLabel' in doc_info:
                                        new_doc_data['documentType'] = doc_info['documentTypeLabel']

                                    nested_docs_list.append(new_doc_data)

                                    flat_doc_data = new_doc_data.copy()
                                    flat_doc_data['meeting_id'] = int(m_id)
                                    flat_doc_data['item_id'] = item_id_int
                                    
                                    doc_ref = db.collection('raadstukken').document(doc_id)
                                    doc_snap_flat = doc_ref.get()
                                    
                                    doc_needs_update = False
                                    if not doc_snap_flat.exists:
                                        doc_needs_update = True
                                    else:
                                        existing_d = doc_snap_flat.to_dict()
                                        for k, v in flat_doc_data.items():
                                            if existing_d.get(k) != v:
                                                doc_needs_update = True
                                                break

                                    if doc_needs_update:
                                        flat_doc_data['last_sync'] = firestore.SERVER_TIMESTAMP
                                        batch.set(doc_ref, flat_doc_data, merge=True)
                                        batch_count += 1
                                        commit_batch_if_needed()
                                        
                                        if is_debug_target: logging.info(f"      -> DOC {doc_id} toegevoegd aan batch.")
                                        
                                        if doc_id not in notified_docs:
                                            send_push_notification("Nieuw raadsstuk gepubliceerd", doc_title)
                                            notified_docs.add(doc_id)
                                            new_docs_found = True

                        except Exception as e:
                            logging.error(f"Fout bij ophalen documenten voor item {item_id}: {e}")
                        
                        nested_item_data['documents'] = nested_docs_list
                        updated_items_list.append(nested_item_data)

                        flat_item_data = {
                            'id': item_id_int,
                            'meeting_id': int(m_id),
                            'title': item_title
                        }
                        item_ref = db.collection('agendapunten').document(item_id)
                        item_snap_flat = item_ref.get()
                        item_needs_update = False
                        
                        if not item_snap_flat.exists:
                            item_needs_update = True
                        else:
                            existing_i = item_snap_flat.to_dict()
                            for k, v in flat_item_data.items():
                                if existing_i.get(k) != v:
                                    item_needs_update = True
                                    break
                                    
                        if item_needs_update:
                            flat_item_data['last_sync'] = firestore.SERVER_TIMESTAMP
                            batch.set(item_ref, flat_item_data, merge=True)
                            batch_count += 1
                            commit_batch_if_needed()

            except Exception as e:
                logging.error(f"Fout bij ophalen items voor meeting {m_id}: {e}")

            new_meeting_data['items'] = updated_items_list

            if not meeting_snap.exists:
                meeting_needs_update = True
            else:
                for k, v in new_meeting_data.items():
                    if str(existing_m.get(k)) != str(v):
                        meeting_needs_update = True
                        if is_debug_target:
                            logging.info(f"MEETING {m_id} VERSCHIL in '{k}':")
                            logging.info(f"  OUD: {str(existing_m.get(k))[:200]}...")
                            logging.info(f"  NIEUW: {str(v)[:200]}...")
                        break

            if meeting_needs_update:
                try:
                    new_meeting_data['last_sync'] = firestore.SERVER_TIMESTAMP
                    batch.set(meeting_ref, new_meeting_data, merge=True)
                    batch_count += 1
                    commit_batch_if_needed()
                    
                    if is_debug_target: logging.info(f"-> MEETING {m_id} toegevoegd aan batch (NESTED STRUCTUUR).")
                    
                    if m_id not in notified_meetings:
                        send_push_notification(f"Nieuwe agenda: {title}", f"Datum: {m_date[:10]}")
                        notified_meetings.add(m_id)
                        new_meetings_found = True
                except Exception as e:
                    logging.error(f"Fout bij opslaan meeting {m_id}: {e}")
            else:
                if is_debug_target: logging.info(f"MEETING {m_id} is ongewijzigd.")

        if processed_meeting_ids:
            logging.info("Start orphaned data cleanup...")
            for m_id_int in processed_meeting_ids:
                items_query = db.collection('agendapunten').where('meeting_id', '==', m_id_int).stream()
                for doc_snap in items_query:
                    if int(doc_snap.id) not in api_item_ids:
                        batch.delete(doc_snap.reference)
                        batch_count += 1

                docs_query = db.collection('raadstukken').where('meeting_id', '==', m_id_int).stream()
                for doc_snap in docs_query:
                    if int(doc_snap.id) not in api_doc_ids:
                        batch.delete(doc_snap.reference)
                        batch_count += 1
                
                commit_batch_if_needed()

        commit_batch_if_needed(force=True)

    except Exception as e:
        logging.error(f"Fout in run_deep_sync: {e}")

    if new_meetings_found:
        save_cache(NOTIFIED_MEETINGS_FILE, notified_meetings)
    if new_docs_found:
        save_cache(NOTIFIED_DOCS_FILE, notified_docs)

if __name__ == "__main__":
    logging.info("--- Start Sync Orchestrator (NESTED MODE & BATCHING) ---")
    run_deep_sync()
    logging.info("--- Eind Sync Orchestrator ---")