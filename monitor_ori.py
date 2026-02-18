import requests
import json
import logging
import os
import sys
import re
import time
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from urllib.parse import quote
from datetime import datetime

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

STATE_FILE = "seen_meetings_firebase.json"
LOG_FILE = "firebase_meetings.log"

# --- FIREBASE SETUP ---
if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# --- LOGGING ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- HELPER FUNCTIES ---

def load_seen_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_seen_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logging.error(f"Fout bij opslaan state: {e}")

# --- FIREBASE LOGICA ---

def push_meeting_to_firebase(meeting_data):
    try:
        meeting_id = str(meeting_data['id'])
        
        # Agenda URL bouwen
        base_url = meeting_data.get('fullUrl', '')
        if base_url.startswith('/'):
            full_agenda_url = f"https://gemeenteraad.dronten.nl{base_url}"
        else:
            full_agenda_url = base_url or "https://gemeenteraad.dronten.nl"

        # Agendapunten verwerken
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

        # Opslaan in Firebase
        doc_ref = db.collection('vergaderingen').document(meeting_id)
        
        meeting_payload = {
            'id': meeting_id,
            'type': meeting_data.get('classification') or meeting_data.get('type') or 'Vergadering',
            'date': meeting_data.get('date'), 
            'location': meeting_data.get('location', 'Onbekend'),
            'url': full_agenda_url,
            'items': items_list,
            'doc_count': total_docs_count,
            'last_updated': firestore.SERVER_TIMESTAMP
        }

        doc_ref.set(meeting_payload, merge=True)
        logging.info(f"Meeting {meeting_id} geüpload ({total_docs_count} docs).")
        return total_docs_count

    except Exception as e:
        logging.error(f"Fout bij uploaden meeting {meeting_data.get('id')}: {e}")
        return 0

# --- MONITOR LOGICA ---

def get_recent_meetings():
    """Haalt meetings op via V2 API met datum filter."""
    current_year = datetime.now().year
    
    # URL Correctie op basis van jouw input: V2, ID descending, datum vanaf 1 jan dit jaar
    url = f"{DRONTEN_API_V2}/meetings/?sort=id_desc&date_from={current_year}-01-01"
    
    try:
        logging.info(f"Ophalen meetings via V2: {url}")
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        
        if resp.status_code == 200:
            data = resp.json()
            
            # V2 Robuuste Parsing (soms 'items', soms 'result'->'items')
            items = []
            if isinstance(data, dict):
                if 'items' in data:
                    items = data['items']
                elif 'result' in data and 'items' in data['result']:
                    items = data['result']['items']
            elif isinstance(data, list):
                items = data
            
            logging.info(f"Aantal meetings gevonden: {len(items)}")
            return items
        else:
            logging.warning(f"API V2 gaf status {resp.status_code}")
            return []
                
    except Exception as e:
        logging.error(f"Fout bij ophalen meetings V2: {e}")
        return []

def run_monitor():
    logging.info("Start Meeting Sync naar Firebase...")
    state = load_seen_state()
    has_changes = False

    # Haal lijst op via nieuwe V2 link
    recent_meetings = get_recent_meetings()
    
    if not recent_meetings:
        logging.warning("Geen vergaderingen gevonden.")
        return

    # Omdat we sort=id_desc gebruiken, staan de nieuwste bovenaan.
    # We checken de bovenste 5 voor wijzigingen.
    meetings_to_check = recent_meetings[:5]

    for meta in meetings_to_check:
        m_id = str(meta['id'])
        
        # Details ophalen (We blijven V1 gebruiken voor details, dat is vaak stabieler voor de nested docs)
        url = f"{DRONTEN_API_V1}/meetings/{m_id}"
        try:
            resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
            if resp.status_code != 200: continue
            
            full_data = resp.json()
            
            # Tel documenten
            raw_items = full_data.get('items') or full_data.get('agenda_items') or []
            current_doc_count = sum(len(i.get('documents', [])) for i in raw_items)
            
            prev_doc_count = state.get(m_id, -1)
            
            # Update bij wijziging
            if current_doc_count != prev_doc_count:
                logging.info(f"Sync meeting {m_id} ({full_data.get('date')})")
                push_meeting_to_firebase(full_data)
                state[m_id] = current_doc_count
                has_changes = True
            
        except Exception as e:
            logging.error(f"Fout bij verwerken detail meeting {m_id}: {e}")

    if has_changes:
        save_seen_state(state)
        logging.info("Sync voltooid. State bijgewerkt.")
    else:
        logging.info("Sync voltooid. Geen wijzigingen.")

if __name__ == "__main__":
    run_monitor()
