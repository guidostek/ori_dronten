# -*- coding: utf-8 -*-
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import time # Nodig voor de 15 minuten wachttijd

# --- NIEUWE IMPORTS VOOR FLASK & ICALENDAR ---
from flask import Flask, Response, abort
from icalendar import Calendar, Event
import pytz
import threading

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
BASE_URL_PUBLIC = "https://gemeenteraad.dronten.nl"

NOTIFIED_MEETINGS_FILE = "/home/guido/oriscript/notified_meetings.json"
NOTIFIED_DOCS_FILE = "/home/guido/oriscript/notified_docs.json"

MY_UID = "Jt7bZksq20QJg3KBPHmm3ij518k1"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- FLASK APP INITIALISATIE (KALENDER ENDPOINT) ---
app = Flask(__name__)

@app.route('/calendar/<user_token>.ics', methods=['GET'])
def get_calendar_feed(user_token):
    # 1. Beveiliging: Verifieer of het token in de Firestore 'users' collectie bestaat
    users_ref = db.collection('users').where('calendarToken', '==', user_token).limit(1).stream()
    user_doc = next(users_ref, None)
    
    if not user_doc:
        abort(401, description="Ongeldig of ontbrekend token. Toegang geweigerd.")
    
    # 2. Start het opbouwen van het iCalendar bestand
    cal = Calendar()
    cal.add('prodid', '-//Dronten Raad App//Agenda Sync//NL')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Raad Dronten')
    cal.add('calscale', 'GREGORIAN')
    
    # 3. Haal vergaderingen op uit de bestaande Firestore 'vergaderingen' collectie
    vergaderingen_ref = db.collection('vergaderingen').stream()
    tz = pytz.timezone("Europe/Amsterdam")
    
    for meeting in vergaderingen_ref:
        data = meeting.to_dict()
        
        m_date_str = data.get('date') # bv. '2023-10-25'
        m_time_str = data.get('startTime', '00:00') # bv. '19:30' of leeg
        
        if not m_date_str:
            continue
            
        try:
            # Converteer de string datum/tijd naar een correct datetime object
            start_dt_str = f"{m_date_str} {m_time_str}"
            
            if not m_time_str.strip():
                # Alleen een datum zonder specifieke starttijd
                start_dt = datetime.strptime(m_date_str, "%Y-%m-%d")
            else:
                start_dt = datetime.strptime(start_dt_str.strip(), "%Y-%m-%d %H:%M")
                
            start_dt = tz.localize(start_dt)
        except (ValueError, TypeError) as e:
            # Sla over als de datum onjuist is opgemaakt
            continue
            
        event = Event()
        event.add('summary', data.get('title', 'Vergadering'))
        event.add('dtstart', start_dt)
        event.add('dtend', start_dt + timedelta(hours=2)) # Standaard 2 uur looptijd
        
        # --- NIEUW: Locatie verrijken met het adres van de gemeente ---
        specifieke_locatie = data.get('location', '').strip()
        standaard_adres = "Huis van de Gemeente Dronten, De Rede 1, 8232 EE Dronten"
        
        if specifieke_locatie:
            # Als er een zaal bekend is (bijv. "Raadzaal"), combineren we dit
            volledige_locatie = f"{specifieke_locatie}, {standaard_adres}"
        else:
            # Geen zaal bekend, gebruik alleen het algemene adres
            volledige_locatie = standaard_adres
            
        event.add('location', volledige_locatie)
        
        # Deep-link naar de app
        meeting_id = meeting.id
        description = f"Bekijk details of claim woordvoerderschap in de app:\nhttps://raaddronten.guidostek.nl/meeting/{meeting_id}"
        event.add('description', description)
        
        # Unieke ID voor de agenda-applicaties om updates te herkennen
        event.add('uid', f"meeting-{meeting_id}@raaddronten.guidostek.nl")
        
        cal.add_component(event)
        
    # 4. Return het bestand in het juiste WebCal formaat
    return Response(
        cal.to_ical(), 
        mimetype='text/calendar', 
        headers={"Content-Disposition": "attachment; filename=raad_dronten.ics"}
    )

# --- HULPFUNCTIES ---
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
            notification=messaging.Notification(title=title, body=body),
            data={'trigger': 'sync_documents', 'document_id': str(doc_id)},
            topic='all_users'
        )
        messaging.send(message)
    except Exception as e:
        print(f"FCM Fout: {e}")

# --- MEDIA SCRAPER: GLOBAAL (MP3) & PER ITEM (MP4 + TIMING) ---
def extract_media_enriched(relative_url, headers, cookies, items_lijst):
    if not relative_url:
        return [], items_lijst
        
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    
    try:
        response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200:
            return meeting_media, items_lijst

        soup = BeautifulSoup(response.text, 'html.parser')

        # 1. GLOBAAL: Zoek de volledige MP3 opname van de vergadering
        for a in soup.find_all('a', href=re.compile(r'\.mp3$', re.I)):
            href = a.get('href')
            url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
            if not any(m['url'] == url for m in meeting_media):
                meeting_media.append({
                    'title': 'Volledige audio-opname',
                    'url': url,
                    'type': 'audio'
                })

        # 2. PER ITEM: Zoek anchors (#) en MP4 fragmenten
        for item in items_lijst:
            item_title = item.get('title', '')
            if not item_title: continue
            
            container = soup.find(lambda tag: tag.name in ['div', 'li', 'tr'] and item_title in tag.get_text())
            
            if container:
                anchor_id = container.get('id')
                if anchor_id:
                    item['anchor'] = f"#{anchor_id}"
                
                item_media = []
                for link in container.find_all('a', href=re.compile(r'\.mp4$', re.I)):
                    href = link.get('href')
                    url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
                    item_media.append({
                        'title': 'Videofragment',
                        'url': url,
                        'type': 'video'
                    })
                
                start_time = container.get('data-start') or container.get('data-time')
                if start_time:
                    item['start_time'] = start_time
                
                item['item_media'] = item_media

    except Exception as e:
        print(f"Scrape fout op {full_url}: {e}")
        
    return meeting_media, items_lijst

# --- HOOFD MONITOR LOGICA ---
def run_monitor():
    cookies = get_user_cookies(MY_UID)
    headers = {'User-Agent': 'Mozilla/5.0'}
    notified_meetings = load_notified(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_notified(NOTIFIED_DOCS_FILE)
    new_meetings_notified = False

    datum_grens = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

    try:
        url = f"{DRONTEN_API_V2}/meetings?limit=40&sort=date_desc"
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        meetings = resp.json().get('result', {}).get('meetings', [])

        for meeting in meetings:
            m_date = meeting.get('date', '')
            if m_date < datum_grens: continue

            m_id = str(meeting['id'])
            items_lijst = [] 
            detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
            try:
                d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=20)
                if d_resp.status_code == 200:
                    items_lijst = d_resp.json().get('items', [])
            except: pass

            # Scrape en verrijk
            m_media, enriched_items = extract_media_enriched(meeting.get('url'), headers, cookies, items_lijst)

            db.collection('vergaderingen').document(m_id).set({
                'id': int(m_id),
                'title': meeting.get('title') or "Vergadering",
                'date': m_date,
                'startTime': meeting.get('startTime', ''),
                'confidential': bool(meeting.get('confidential', 0)),
                'items': enriched_items,
                'media_attachments': m_media, 
                'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                'location': meeting.get('location', ''), 
                'last_sync': firestore.SERVER_TIMESTAMP
            }, merge=True)

            if m_id not in notified_meetings:
                send_push_notification(f"Nieuwe agenda: {meeting.get('title')}", f"Datum: {m_date[:10]}")
                notified_meetings.add(m_id)
                new_meetings_notified = True

        if new_meetings_notified:
            save_notified(NOTIFIED_MEETINGS_FILE, notified_meetings)

    except Exception as e:
        print(f"Monitor Fout: {e}")

# --- NIEUWE FUNCTIE VOOR DE 15-MINUTEN LOOP ---
def monitor_loop():
    while True:
        try:
            print(f"[{datetime.now()}] Start geplande monitor taak...")
            run_monitor()
            print(f"[{datetime.now()}] Monitor taak klaar. Wachten voor 15 minuten (900 sec)...")
        except Exception as e:
            print(f"[{datetime.now()}] Fout in monitor loop: {e}")
        
        # Slaap voor 15 minuten (900 seconden) voordat hij weer opnieuw begint
        time.sleep(900) 

if __name__ == "__main__":
    # 1. Start de herhalende monitor-lus in de achtergrond
    print("Start de monitor taak in de achtergrond (elke 15 minuten)...")
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    
    # 2. Start de Flask server om de iCalendar bestanden continu te serveren
    print("Start de Flask webserver voor /calendar/ endpoint op poort 5000...")
    # Pas host/port aan indien nodig voor jouw specifieke web-omgeving (zoals NGINX reverse proxy)
    app.run(host='0.0.0.0', port=5000)