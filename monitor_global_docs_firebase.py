import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, messaging

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
NOTIFIED_DOCS_FILE = "/home/guido/oriscript/notified_docs.json"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

def load_notified_docs():
    if os.path.exists(NOTIFIED_DOCS_FILE):
        try:
            with open(NOTIFIED_DOCS_FILE, 'r') as f: return set(json.load(f))
        except: return set()
    return set()

def save_notified_docs(notified_set):
    with open(NOTIFIED_DOCS_FILE, 'w') as f: json.dump(list(notified_set), f)

def send_push_notification(title, body):
    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            topic='raad_updates',
        )
        messaging.send(message)
    except Exception as e:
        print(f"FCM Fout: {e}")

def run_monitor():
    notified_docs = load_notified_docs()
    new_notifications = False
    
    # Haal recente stukken op (we pakken er 50 om ook herstel te bieden)
    url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
    resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
    if resp.status_code != 200: return
    
    docs = resp.json().get('result', {}).get('documents') or []
    
    for doc in docs:
        doc_id = str(doc['id'])
        
        # Verbeterde naamgeving logica:
        # We proberen description, dan filename, dan original_filename
        title = doc.get('description') or doc.get('filename') or doc.get('original_filename') or f"Document {doc_id}"
        
        # 1. Sync naar Firestore
        doc_ref = db.collection('raadstukken').document(doc_id)
        doc_ref.set({
            'id': doc_id,
            'title': title,
            'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
            'timestamp': firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        # 2. Notificatie check
        if doc_id not in notified_docs:
            send_push_notification("Nieuw raadsstuk", title)
            notified_docs.add(doc_id)
            new_notifications = True
            
    if new_notifications:
        save_notified_docs(notified_docs)
    print(f"Monitor voltooid. {len(docs)} stukken gecontroleerd/bijgewerkt.")

if __name__ == "__main__":
    run_monitor()
