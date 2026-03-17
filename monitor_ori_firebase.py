# -*- coding: utf-8 -*-
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
BASE_URL_PUBLIC = "https://gemeenteraad.dronten.nl"

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

def send_push_notification(title, body, doc_id=""):
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body
            ),
            data={
                'trigger': 'sync_documents',
                'document_id': str(doc_id)
            },
            topic='all_users'
        )
        messaging.send(message)        
        print(f"Notificatie verstuurd: {title}")
    except Exception as e:
        print(f"FCM Fout bij sturen van notificatie: {e}")

# --- NIEUW: MEDIA SCRAPER ---

def extract_media_from_url(relative_url, headers, cookies):
    """
    Scrapet de publieke vergaderpagina voor audio/video links.
    """
    if not relative_url:
        return []
        
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    media_links = []
    
    try:
        response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200:
            return media_links

        soup = BeautifulSoup(response.text, 'html.parser')

        # 1. Zoek naar iFrames (Notubiz / Videoverslagen)
        iframes = soup.find_all('iframe')
        for iframe in iframes:
            src = iframe.get('src')
            if src:
                if any(provider in src for provider in ['notubiz', 'youtube', 'vimeo', 'raadsinformatie']):
                    media_links.append({
                        'title': 'Videoverslag / Stream',
                        'url': src if src.startswith('http') else f"https:{src}",
                        'type': 'video'
                    })

        # 2. Zoek naar directe audio/video bestanden (.mp3, .mp4)
        file_links = soup.find_all('a', href=re.compile(r'\.(mp3|mp4|m4a)$', re.I))
        for link in file_links:
            href = link.get('href')
            title = link.get_text(strip=True) or "Media bestand"
            if href.startswith('/'):
                href = f"{BASE_URL_PUBLIC}{href}"
            
            media_links.append({
                'title': title,
                'url': href,
                'type': 'audio' if href.lower().endswith('.mp3') else 'video'
            })

    except Exception as e:
        print(f"[-] Fout tijdens scrapen van {full_url}: {e}")
        
    return media_links

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
            relative_url = meeting.get('url')

            # --- NIEUW: MEDIA EXTRACTIE ---
            media_attachments = extract_media_from_url(relative_url, headers, cookies)

            # --- HAAL DETAILS OP (VOOR DE AGENDAPUNTEN) ---
            items_lijst = [] 
            detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
            try:
                d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=20)
                if d_resp.status_code == 200:
                    items_lijst = d_resp.json().get('items', [])
            except Exception as e:
                print(f"Fout bij ophalen details voor {m_id}: {e}")

            # Sla de vergadering op INCLUSIEF media_attachments
            db.collection('vergaderingen').document(m_id).set({
                'id': int(m_id),
                'title': title,
                'date': m_date,
                'startTime': meeting.get('startTime', ''),
                'confidential': is_geheim,
                'description': meeting.get('description', ''),
                'dmu': meeting.get('dmu'),
                'meetingLabel': meeting.get('meetingLabel'),
                'location': meeting.get('location'),
                'items': items_lijst,
                'media_attachments': media_attachments, # <--- Hier worden de links opgeslagen
                'url_public': f"{BASE_URL_PUBLIC}{relative_url}" if relative_url else "",
                'last_sync': firestore.SERVER_TIMESTAMP
            }, merge=True)

            # --- CHECK VOOR PUSH NOTIFICATIE ---
            if m_id not in notified_meetings:
                total_docs = sum(len(i.get('documents', [])) for i in items_lijst)

                if total_docs > 0:
                    status_label = "[BESLOTEN] " if is_geheim else ""
                    titel_notif = f"Nieuwe agenda: {status_label}{title}"
                    body_notif = f"Datum: {m_date[:10]} met {total_docs} documenten beschikbaar."

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
            
            if doc_id not in notified_docs:
                status_label = "[BESLOTEN] " if is_geheim else ""
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