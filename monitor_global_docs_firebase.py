import requests
import json
import logging
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DOCUMENTS_API_URL = "https://gemeenteraad.dronten.nl/api/v2/documents?sort=id_desc&limit=20"
DOWNLOAD_BASE_URL = "https://gemeenteraad.dronten.nl/api/v2/documents"

STATE_FILE = "seen_docs_firebase.json"
LOG_FILE = "firebase_backend.log"

# Zet dit op True om altijd 1 melding te forceren zonder de boel te ontregelen
TEST_MODE = True

# --- FIREBASE SETUP ---
if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# --- LOGGING ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- STATE FUNCTIES ---
def load_seen_docs():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return set(json.load(f))
        except:
            pass
    return set()

def save_seen_docs(seen_ids):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(list(seen_ids), f)
    except Exception as e:
        logging.error(f"Kon state file niet opslaan: {e}")

# --- FIREBASE & NOTIFICATIE LOGICA ---
def push_to_firebase(new_documents):
    if not new_documents: return
    
    batch = db.batch() 
    count = 0
    laatste_doc_titel = ""

    for doc in new_documents:
        doc_id = str(doc['id'])
        filename = doc.get('fileName') or doc.get('filename') or 'Naamloos document'
        laatste_doc_titel = filename
        
        download_url = f"{DOWNLOAD_BASE_URL}/{doc_id}/download"
        
        doc_data = {
            'doc_id': doc_id,
            'title': filename,
            'url': download_url,
            'timestamp': firestore.SERVER_TIMESTAMP,
            'source': 'Gemeente Dronten',
            'type': 'global_doc',
            'is_read': False
        }
        
        doc_ref = db.collection('raadstukken').document(doc_id)
        batch.set(doc_ref, doc_data)
        count += 1

    try:
        # 1. Schrijf naar de database
        batch.commit()
        logging.info(f"Succes: {count} documenten naar Firebase geüpload.")
        
        # 2. Stuur de Push Notificatie
        if count > 0:
            titel = "Nieuw Raadstuk"
            body = f"Er zijn {count} nieuwe stukken. Laatste: {laatste_doc_titel}"
            if count == 1:
                body = laatste_doc_titel

            # Bericht opmaken voor het juiste topic
            message = messaging.Message(
                notification=messaging.Notification(
                    title=titel,
                    body=body,
                ),
                topic='raad_updates',
            )
            
            # Bericht daadwerkelijk afvuren
            response = messaging.send(message)
            logging.info(f"Push notificatie verstuurd: {response}")

    except Exception as e:
        logging.error(f"Fout bij uploaden naar Firebase of FCM: {e}")

# --- API MONITOR LOGICA ---
def check_documents():
    logging.info("Start Firebase Docs Sync...")
    seen_ids = load_seen_docs()
    
    try:
        resp = requests.get(DOCUMENTS_API_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        
        if resp.status_code != 200:
            logging.error(f"API Fout: {resp.status_code}")
            return

        data = resp.json()
        items = []

        # Robuuste JSON parsing
        if isinstance(data, dict):
            if 'result' in data and 'documents' in data['result']:
                items = data['result']['documents']
            elif 'documents' in data:
                items = data['documents']
            elif 'items' in data:
                items = data['items']
        elif isinstance(data, list):
            items = data
            
        new_batch = []
        for doc in items:
            doc_id = str(doc['id'])
            if doc_id not in seen_ids or TEST_MODE:
                new_batch.append(doc)
                seen_ids.add(doc_id)
                if TEST_MODE: break # Bij test mode maar 1 item verwerken om spam te voorkomen
        
        if new_batch:
            logging.info(f"{len(new_batch)} nieuwe items gevonden.")
            push_to_firebase(new_batch)
            if not TEST_MODE:
                save_seen_docs(seen_ids)
        else:
            logging.info("Geen nieuwe items voor Firebase.")

    except Exception as e:
        logging.error(f"Algemene fout: {e}")

if __name__ == "__main__":
    check_documents()
