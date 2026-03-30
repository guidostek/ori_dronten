# -*- coding: utf-8 -*-
import requests
import json
import os
import logging
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

def sync_public_meetings():
    """Haalt vergaderingen op, schrijft naar Firestore en verstuurt pings bij nieuwe items."""
    notified = load_cache(NOTIFIED_MEETINGS_FILE)
    url = f"{DRONTEN_API_V2}/meetings?sort=date_desc&limit={LIMIT_MEETINGS}"
    
    try:
        resp = requests.get(url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logging.error(f"API weigerde toegang tot meetings: {resp.status_code}")
            return

        meetings = resp.json().get('result', {}).get('meetings', [])
        new_items_found = False

        for meeting in meetings:
            m_id = str(meeting['id'])
            m_date = meeting.get('date', '')
            title = meeting.get('title') or "Vergadering"

            # Schrijf weg naar Firestore
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

            # Controleer voor notificatie
            if m_id not in notified:
                send_push_notification(f"Nieuwe agenda: {title}", f"Datum: {m_date[:10]}")
                notified.add(m_id)
                new_items_found = True

        if new_items_found:
            save_cache(NOTIFIED_MEETINGS_FILE, notified)
            logging.info("Meeting cache bijgewerkt.")

    except Exception as e:
        logging.error(f"Fout in sync_public_meetings: {e}")

def sync_public_documents():
    """Haalt losse openbare documenten op, schrijft naar Firestore en verstuurt pings bij nieuwe documenten."""
    notified = load_cache(NOTIFIED_DOCS_FILE)
    url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit={LIMIT_DOCS}"
    
    try:
        resp = requests.get(url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logging.error(f"API weigerde toegang tot documents: {resp.status_code}")
            return

        docs = resp.json().get('result', {}).get('documents', [])
        new_items_found = False

        for doc in docs:
            doc_id = str(doc['id'])
            title = doc.get('description') or doc.get('filename') or f"Stuk {doc_id}"
            
            # Schrijf weg naar Firestore
            db.collection('raadstukken').document(doc_id).set({
                'id': doc_id,
                'title': title,
                'confidential': doc.get('confidential', False),
                'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
                'last_sync': firestore.SERVER_TIMESTAMP
            }, merge=True, timeout=15)

            # Controleer voor notificatie (Hiermee is het lek gedicht!)
            if doc_id not in notified:
                send_push_notification("Nieuw raadsstuk gepubliceerd", title)
                notified.add(doc_id)
                new_items_found = True

        if new_items_found:
            save_cache(NOTIFIED_DOCS_FILE, notified)
            logging.info("Document cache bijgewerkt.")

    except Exception as e:
        logging.error(f"Fout in sync_public_documents: {e}")

if __name__ == "__main__":
    logging.info("--- Start Sync Orchestrator ---")
    sync_public_meetings()
    sync_public_documents()
    logging.info("--- Eind Sync Orchestrator ---")