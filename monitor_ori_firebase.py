# -*- coding: utf-8 -*-
import requests
import json
import logging
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from urllib.parse import quote
from datetime import datetime, timedelta

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

STATE_FILE = "/home/guido/oriscript/seen_meetings_firebase.json"
NOTIFIED_FILE = "/home/guido/oriscript/notified_meetings.json"
LOG_FILE = "/home/guido/oriscript/firebase_meetings.log"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

def load_json_file(path):
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json_file(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)

def send_push_notification(title, body):
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            topic='raad_updates',
        )
        response = messaging.send(message)
        logging.info(f"Push notificatie verzonden: {response}")
    except Exception as e:
        logging.error(f"Fout bij verzenden push: {e}")

def push_meeting_to_firebase(meta_data, full_data=None):
    try:
        meeting_id = str(meta_data['id'])
        
        dmu_name = 'Vergadering'
        if meta_data.get('dmu'):
            dmu_name = meta_data['dmu'].get('name', 'Vergadering')
            
        label_val = None
        if meta_data.get('meetingLabel'):
            label_val = meta_data['meetingLabel'].get('value')
            
        display_type = f"{dmu_name} - {label_val}" if label_val else dmu_name
        meeting_date = meta_data.get('date')
        
        meeting_time = meta_data.get('startTime') or meta_data.get('time') or ''

        doc_ref = db.collection('vergaderingen').document(meeting_id)
        existing_doc = doc_ref.get()
        
        meeting_payload = {
            'id': meeting_id,
            'type': display_type,
            'date': meeting_date,
            'startTime': meeting_time, 
            'location': meta_data.get('location', 'Onbekend'),
            'last_updated': firestore.SERVER_TIMESTAMP,
        }

        if existing_doc.exists:
            current_db_data = existing_doc.to_dict()
            meeting_payload['synced'] = current_db_data.get('synced', True)
        else:
            meeting_payload['synced'] = False

        if full_data:
            items_list = []
            total_docs = 0
            raw_items = full_data.get('items') or full_data.get('agenda_items', []) or []
            
            for item in raw_items:
                if not item: continue
                docs = []
                for d in (item.get('documents') or []):
                    if not d: continue
                    d_id = str(d.get('id', ''))
                    if not d_id: continue
                    dl_link = f"{DRONTEN_API_V2}/documents/{d_id}/download"
                    docs.append({
                        'id': d_id,
                        'filename': d.get('filename', 'Naamloos'),
                        'url': dl_link,
                        'viewer_url': f"https://docs.google.com/viewer?url={quote(dl_link)}&embedded=true"
                    })
                    total_docs += 1
                
                # FIX: Haal toelichting uit ALLE mogelijke API velden
                raw_desc = item.get('explanation') or item.get('description') or item.get('text') or ''
                
                items_list.append({
                    'number': str(item.get('number', '')),
                    'title': item.get('title', 'Agendapunt'),
                    'description': str(raw_desc).strip(), 
                    'documents': docs
                })
            
            meeting_payload.update({
                'items': items_list,
                'doc_count': total_docs,
                'synced': True
            })

        doc_ref.set(meeting_payload, merge=True)
        return display_type, meeting_date
    except Exception as e:
        logging.error(f"Fout in push_meeting_to_firebase voor {meta_data.get('id')}: {e}")
        return None, None

def run_monitor():
    logging.info("--- Start Firebase Meeting Monitor ---")
    
    seen_state = load_json_file(STATE_FILE) 
    notified_ids = set(load_json_file(NOTIFIED_FILE)) 

    now = datetime.now()
    start_date_str = (now - timedelta(days=60)).strftime('%Y-%m-%d')
    
    sync_start = now - timedelta(days=14)
    sync_end = now + timedelta(days=42)

    url = f"{DRONTEN_API_V2}/meetings?sort=date_asc&date_from={start_date_str}&limit=100"
    
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        data = resp.json()
        items = data.get('result', {}).get('meetings') or data.get('items') or []
        
        has_new_notif = False

        for meta in items:
            m_id = str(meta['id'])
            m_date_raw = meta.get('date', '')
            if not m_date_raw: continue
            
            m_date_dt = datetime.strptime(m_date_raw[:10], '%Y-%m-%d')
            
            full_data = None
            if sync_start <= m_date_dt <= sync_end:
                detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
                d_resp = requests.get(detail_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                if d_resp.status_code == 200:
                    full_data = d_resp.json()

            display_type, m_date = push_meeting_to_firebase(meta, full_data)

            if full_data and m_id not in notified_ids:
                total_docs = sum(len(i.get('documents', [])) for i in (full_data.get('items') or []))
                
                if total_docs > 0:
                    titel_notif = f"Nieuwe agenda: {display_type}"
                    body_notif = f"Datum: {m_date[:10]} met {total_docs} documenten beschikbaar."
                    send_push_notification(titel_notif, body_notif)
                    
                    notified_ids.add(m_id)
                    has_new_notif = True

        if has_new_notif:
            save_json_file(NOTIFIED_FILE, list(notified_ids))
            logging.info("Notificatie geheugen bijgewerkt.")

    except Exception as e:
        logging.error(f"Fout tijdens run_monitor: {e}")

if __name__ == "__main__":
    run_monitor()