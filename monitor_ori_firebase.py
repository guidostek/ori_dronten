# -*- coding: utf-8 -*-
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime, timedelta

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

# Bestanden om bij te houden waarvoor al een push is verstuurd
NOTIFIED_MEETINGS_FILE = "/home/guido/oriscript/notified_meetings.json"
NOTIFIED_DOCS_FILE = "/home/guido/oriscript/notified_docs.json"

MY_UID = "Jt7bZksq20QJg3KBPHmm3ij518k1"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- HULPFUNCTIES VOOR NOTIFICATIES EN COOKIES ---

def get_user_cookies(uid):
    session_path = os.path.join(SESSION_DIR, f"{uid}.json")
    if os.path.exists(session_path):
        with open(session_path, 'r') as f:
            return json.load(f).get('cookies', {})
    return None

def load_notified(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return set(json.load(f))
        except: return set()
    return set()

def save_notified(filepath, data_set):
    with open(filepath, 'w') as f:
        json.dump(list(data_set), f)

# Proactief opgelost: doc_id als optionele parameter toegevoegd
def send_push_notification(title, body, doc_id=""):
    try:
        message = messaging.Message(
            # Zorgt voor de zichtbare pop-up op de telefoon
            notification=messaging.Notification(
                title=title,
                body=body
            ),
            # Zorgt voor de silent push / data trigger op de achtergrond
            data={
                'trigger': 'sync_documents',
                'document_id': str(doc_id)
            },
            topic='all_users' # Of naar specifieke FCM tokens
        )
        messaging.send(message)        
        print(f"Notificatie verstuurd: {title}")
    except Exception as e:
        print(f"FCM Fout bij sturen van notificatie: {e}")

# --- HOOFD MONITOR LOGICA ---

def run_monitor():
    cookies = get_user_cookies(MY_UID)
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Laad de geschiedenis van notificaties
    notified_meetings = load_notified(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_notified(NOTIFIED_DOCS_FILE)
    
    new_meetings_notified = False
    new_docs_notified = False

    datum_grens = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

    # --- DEEL 1: VERGADERINGEN ---
    try:
        url = f"{DRONTEN_API_V2}/meetings?limit=40&sort=date_desc"
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        meetings = resp.json().get('result', {}).get('meetings', [])

        for meeting in meetings:
            m_date = meeting.get('date', '')
            if m_date < datum_grens:
                continue

            m_id = str(meeting['id'])
            title = meeting.get('title') or "Vergadering"
            is_geheim = bool(meeting.get('confidential', 0))

            # --- HAAL DETAILS OP (VOOR DE AGENDAPUNTEN) ---
            items_lijst = [] # Fallback: altijd een lege lijst als het mislukt
            detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
            try:
                d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=20)
                if d_resp.status_code == 200:
                    items_lijst = d_resp.json().get('items', [])
            except Exception as e:
                print(f"Fout bij ophalen details voor {m_id}: {e}")

            # Sla de vergadering op INCLUSIEF de agendapunten (items)
            db.collection('vergaderingen').document(m_id).set({
                'id': int(m_id),
                'title': title,
                'date': m_date,
                'confidential': is_geheim,
                'items': items_lijst, # <--- De lijst met punten gaat hier het document in
                'last_sync': firestore.SERVER_TIMESTAMP
            }, merge=True)

            # --- CHECK VOOR PUSH NOTIFICATIE ---
            if m_id not in notified_meetings:
                # Tel documenten in de zojuist opgehaalde items_lijst
                total_docs = sum(len(i.get('documents', [])) for i in items_lijst)

                if total_docs > 0:
                    status_label = "[BESLOTEN] " if is_geheim else ""
                    titel_notif = f"Nieuwe agenda: {status_label}{title}"
                    body_notif = f"Datum: {m_date[:10]} met {total_docs} documenten beschikbaar."

                    # Geeft geen doc_id mee, want dit is een vergadering
                    send_push_notification(titel_notif, body_notif)
                    notified_meetings.add(m_id)
                    new_meetings_notified = True

        if new_meetings_notified:
            save_notified(NOTIFIED_MEETINGS_FILE, notified_meetings)

    except Exception as e:
        print(f"Fout bij agenda sync: {e}")

    # --- DEEL 2: RAADSTUKKEN (Global Docs) ---
    try:
        url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        docs = resp.json().get('result', {}).get('documents', [])

        for doc in docs:
            doc_id = str(doc['id'])
            is_geheim = bool(doc.get('confidential', 0))
            title = doc.get('description') or doc.get('filename') or doc.get('original_filename') or f"Stuk {doc_id}"

            db.collection('raadstukken').document(doc_id).set({
                'id': int(doc_id),
                'title': title,
                'confidential': is_geheim,
                'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
                'timestamp': firestore.SERVER_TIMESTAMP
            }, merge=True)
            
            # Check voor notificatie van losse nieuwe documenten
            if doc_id not in notified_docs:
                status_label = "[BESLOTEN] " if is_geheim else ""
                
                # Proactieve aanpassing: Hier sturen we de doc_id wél mee!
                send_push_notification(
                    title="Nieuw Raadstuk geplaatst", 
                    body=f"{status_label}{title}",
                    doc_id=doc_id 
                )
                notified_docs.add(doc_id)
                new_docs_notified = True

        if new_docs_notified:
            save_notified(NOTIFIED_DOCS_FILE, notified_docs)

    except Exception as e:
        print(f"Fout bij docs sync: {e}")

if __name__ == "__main__":
    run_monitor()