import requests
import json
import logging
import os
import sys
import re
import firebase_admin
from firebase_admin import credentials, firestore
from urllib.parse import quote
from datetime import datetime, timedelta

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

STATE_FILE = "seen_meetings_firebase.json"
LOG_FILE = "firebase_meetings.log"

# TEST_MODE: True = Forceer een update van de eerste meeting in het venster
TEST_MODE = False

# --- FIREBASE SETUP ---
if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- LOGGING ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- FIREBASE LOGICA ---

def push_meeting_to_firebase(meeting_data):
    try:
        meeting_id = str(meeting_data['id'])
        base_url = meeting_data.get('fullUrl', '')
        full_agenda_url = f"https://gemeenteraad.dronten.nl{base_url}" if base_url.startswith('/') else base_url

        items_list = []
        raw_items = meeting_data.get('items') or meeting_data.get('agenda_items', [])
        total_docs_count = 0

        for item in raw_items:
            docs = []
            for d in item.get('documents', []):
                doc_id = str(d['id'])
                dl_link = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
                docs.append({
                    'id': doc_id,
                    'filename': d.get('filename', 'Naamloos'),
                    'url': dl_link,
                    'viewer_url': f"https://docs.google.com/viewer?url={quote(dl_link)}&embedded=true"
                })
                total_docs_count += 1
            items_list.append({
                'number': str(item.get('number', '')),
                'title': item.get('title', 'Agendapunt'),
                'description': item.get('description', ''),
                'documents': docs
            })

        doc_ref = db.collection('vergaderingen').document(meeting_id)
        doc_ref.set({
            'id': meeting_id,
            'type': meeting_data.get('classification') or meeting_data.get('type') or 'Vergadering',
            'date': meeting_data.get('date'), 
            'location': meeting_data.get('location', 'Onbekend'),
            'url': full_agenda_url,
            'items': items_list,
            'doc_count': total_docs_count,
            'last_updated': firestore.SERVER_TIMESTAMP
        }, merge=True)
        logging.info(f"Meeting {meeting_id} gesynced naar Firebase.")
        return total_docs_count
    except Exception as e:
        logging.error(f"Fout bij Firebase upload {meeting_data.get('id')}: {e}")
        return 0

# --- MONITOR LOGICA ---

def run_monitor():
    logging.info("--- Start Firebase Meeting Sync (Venster 6 weken) ---")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: state = json.load(f)
    else: state = {}
    
    now = datetime.now()
    # Bereken datums voor het Python filter
    dt_from = now - timedelta(days=14)
    dt_to = now + timedelta(days=30)
    
    # Gebruik de URL structuur die werkt: sort op id_desc en pak een ruime limit
    url = f"{DRONTEN_API_V2}/meetings/?sort=id_desc&date_from={dt_from.strftime('%Y-%m-%d')}&limit=100"
    
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        if resp.status_code != 200: return
        
        data = resp.json()
        items = data.get('items') or data.get('result', {}).get('items', [])
        
        has_changes = False
        for meta in items:
            m_id = str(meta['id'])
            m_date_str = meta.get('date', '')
            if not m_date_str: continue
            
            # Python check: valt de datum binnen ons 6-weken venster?
            m_date = datetime.strptime(m_date_str[:10], '%Y-%m-%d')
            if not (dt_from <= m_date <= dt_to):
                continue

            # Als hij in het venster valt, haal de details op
            detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
            d_resp = requests.get(detail_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
            if d_resp.status_code != 200: continue
            
            full_data = d_resp.json()
            raw_items = full_data.get('items') or full_data.get('agenda_items', [])
            current_doc_count = sum(len(i.get('documents', [])) for i in raw_items)
            
            if state.get(m_id) != current_doc_count or TEST_MODE:
                push_meeting_to_firebase(full_data)
                state[m_id] = current_doc_count
                has_changes = True
                if TEST_MODE: break

        if has_changes:
            with open(STATE_FILE, 'w') as f: json.dump(state, f)
            
    except Exception as e:
        logging.error(f"Fout bij sync: {e}")

if __name__ == "__main__":
    run_monitor()
