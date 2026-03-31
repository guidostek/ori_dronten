# -*- coding: utf-8 -*-
import requests
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore
from config import *
from firebase_client import db
from notification_service import send_push_notification

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
    """Laadt de lijst met reeds genotificeerde ID's."""
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def save_cache(filepath, data_set):
    """Slaat de actuele lijst met genotificeerde ID's op."""
    with open(filepath, 'w') as f:
        json.dump(list(data_set), f)

def get_sync_start_date_str():
    """Berekent de startdatum (3 maanden terug) en geeft dit in YYYY-MM-DD formaat."""
    start_date = datetime.now(timezone.utc) - timedelta(days=DEEP_SYNC_DAYS)
    return start_date.strftime('%Y-%m-%d')

def extract_data(response_json, fallback_key):
    """Extraheert veilig de data array uit de API v2 response."""
    result = response_json.get('result', {})
    if 'model' in result:
        return result['model']
    return result.get(fallback_key, [])

def run_deep_sync():
    """Voert de exacte trapsgewijze deep sync uit volgens de API V2 documentatie."""
    date_from_str = get_sync_start_date_str()
    logging.info(f"Start Deep Sync vanaf datum: {date_from_str}")

    notified_meetings = load_cache(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_cache(NOTIFIED_DOCS_FILE)
    
    new_meetings_found = False
    new_docs_found = False

    # STAP 1: MEETINGS OPHALEN
    # We gebruiken de date_from parameter uit de API documentatie om efficiënt 3 maanden op te halen
    meetings_url = f"{DRONTEN_API_V2}/meetings?date_from={date_from_str}&sort=date_desc&limit={LIMIT_MEETINGS}"
    
    try:
        resp = requests.get(meetings_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logging.error(f"API weigerde toegang tot meetings: {resp.status_code}")
            return

        meetings = extract_data(resp.json(), 'meetings')

        for meeting in meetings:
            m_id = str(meeting.get('id'))
            if not m_id or m_id == 'None':
                continue
                
            m_date = meeting.get('date', '')
            title = meeting.get('title') or "Vergadering"

            # 1.1 Schrijf Meeting weg naar Firestore
            db.collection('vergaderingen').document(m_id).set({
                'id': int(m_id),
                'title': title,
                'date': m_date,
                'startTime': meeting.get('startTime', ''),
                'confidential': bool(meeting.get('confidential', 0)),
                'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                'location': meeting.get('location', ''), 
                'last_sync': firestore.SERVER_TIMESTAMP
            }, merge=True, timeout=15)

            # 1.2 Notificatie voor nieuwe meeting
            if m_id not in notified_meetings:
                send_push_notification(f"Nieuwe agenda: {title}", f"Datum: {m_date[:10]}")
                notified_meetings.add(m_id)
                new_meetings_found = True

            # STAP 2: MEETING ITEMS OPHALEN PER MEETING
            items_url = f"{DRONTEN_API_V2}/meetings/{m_id}/meetingitems"
            
            try:
                items_resp = requests.get(items_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
                if items_resp.status_code == 200:
                    items = extract_data(items_resp.json(), 'items')
                    
                    for item in items:
                        item_id = str(item.get('id'))
                        if not item_id or item_id == 'None':
                            continue
                        
                        # STAP 3: DOCUMENTEN OPHALEN PER MEETING ITEM
                        docs_url = f"{DRONTEN_API_V2}/meetingitems/{item_id}/documents"
                        
                        try:
                            docs_resp = requests.get(docs_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
                            if docs_resp.status_code == 200:
                                docs = extract_data(docs_resp.json(), 'documents')
                                
                                for doc in docs:
                                    doc_id = str(doc.get('id'))
                                    if not doc_id or doc_id == 'None':
                                        continue
                                        
                                    doc_title = doc.get('fileName') or doc.get('description') or f"Stuk {doc_id}"
                                    
                                    # STAP 4: DOWNLOAD URL GENEREREN
                                    download_url = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
                                    
                                    # 4.1 Schrijf Document weg naar Firestore
                                    db.collection('raadstukken').document(doc_id).set({
                                        'id': int(doc_id),
                                        'title': doc_title,
                                        'meeting_id': int(m_id),
                                        'item_id': int(item_id),
                                        'confidential': bool(doc.get('confidential', 0)),
                                        'url': download_url,
                                        'last_sync': firestore.SERVER_TIMESTAMP
                                    }, merge=True, timeout=15)

                                    # 4.2 Notificatie voor nieuw document
                                    if doc_id not in notified_docs:
                                        send_push_notification("Nieuw raadsstuk gepubliceerd", doc_title)
                                        notified_docs.add(doc_id)
                                        new_docs_found = True
                        except Exception as e:
                             logging.error(f"Fout bij ophalen docs voor item {item_id}: {e}")
            except Exception as e:
                logging.error(f"Fout bij ophalen items voor meeting {m_id}: {e}")

    except Exception as e:
        logging.error(f"Fout in run_deep_sync: {e}")

    # Caches opslaan indien gewijzigd
    if new_meetings_found:
        save_cache(NOTIFIED_MEETINGS_FILE, notified_meetings)
        logging.info("Meeting cache bijgewerkt.")
    if new_docs_found:
        save_cache(NOTIFIED_DOCS_FILE, notified_docs)
        logging.info("Document cache bijgewerkt.")

if __name__ == "__main__":
    logging.info("--- Start Sync Orchestrator ---")
    run_deep_sync()
    logging.info("--- Eind Sync Orchestrator ---")