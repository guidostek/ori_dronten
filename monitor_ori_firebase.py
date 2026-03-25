# -*- coding: utf-8 -*-
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import logging

# Maak de log-map aan als deze nog niet bestaat
os.makedirs("/home/guido/logs", exist_ok=True)

# --- GLOBALE LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/guido/logs/monitor_ori.log"),
        logging.StreamHandler()
    ]
)

logging.info("--- Start ORI Monitor Run (Cronjob) ---")

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
BASE_URL_PUBLIC = "https://gemeenteraad.dronten.nl"

NOTIFIED_MEETINGS_FILE = "/home/guido/oriscript/notified_meetings.json"
NOTIFIED_DOCS_FILE = "/home/guido/oriscript/notified_docs.json"

MY_UID = "Jt7bZksq20QJg3KBPHmm3ij518k1"

# --- FIREBASE INITIALISATIE ---
db = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(CRED_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Verbinden met Firebase geslaagd.")
except Exception as e:
    logging.error(f"FATALE FOUT bij Firebase initialisatie: {e}")

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

# --- Notificaties ---
def send_push_notification(title, body, doc_id=""):
    try:
        logging.info(f"Start versturen push notificatie: {title}")
        
        # Controleer of we toegang hebben tot de database
        global db
        if db is None:
            logging.error("Kan notificatie niet versturen: Geen Firebase database verbinding actief.")
            return

        # Stap 1: Haal alle geregistreerde tokens op
        users_ref = db.collection('users').stream()
        tokens = []
        for user in users_ref:
            user_data = user.to_dict()
            token = user_data.get('fcmToken')
            if token:
                tokens.append(token)
                
        if not tokens:
            logging.warning("Geen FCM tokens gevonden in de 'users' collectie. Notificatie geannuleerd.")
            return

        logging.info(f"Gevonden FCM tokens: {len(tokens)}. Bericht wordt nu klaargezet...")

        # Stap 2 & 3: Verstuur berichten één-voor-één
        success_count = 0
        failure_count = 0
        
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={'trigger': 'sync_documents', 'document_id': str(doc_id)},
                    token=token
                )
                messaging.send(message)
                success_count += 1
            except Exception as e:
                logging.error(f"Fout bij versturen naar token {token}: {e}")
                failure_count += 1

        # Stap 4: Log de resultaten
        logging.info(f"Push notificatie verzonden. Succes: {success_count}, Gefaald: {failure_count}")

    except Exception as e:
        logging.error(f"Fatale FCM Fout in de verzend-keten: {e}", exc_info=True)

# --- MEDIA SCRAPER ---
def extract_media_enriched(relative_url, headers, cookies, items_lijst):
    if not relative_url:
        return [], items_lijst
        
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    
    try:
        # CRUCIALE FIX: Timeout voorkomt deadlock bij scrapen
        response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200:
            return meeting_media, items_lijst

        soup = BeautifulSoup(response.text, 'html.parser')

        for a in soup.find_all('a', href=re.compile(r'\.mp3$', re.I)):
            href = a.get('href')
            url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
            if not any(m['url'] == url for m in meeting_media):
                meeting_media.append({
                    'title': 'Volledige audio-opname',
                    'url': url,
                    'type': 'audio'
                })

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
        logging.error(f"Scrape fout op {full_url}: {e}")
        
    return meeting_media, items_lijst

# --- HOOFD MONITOR LOGICA ---
def run_monitor():
    logging.info("DEBUG: We zijn binnen in run_monitor!")
    if db is None:
        logging.error("Kan monitor niet draaien: Geen Firebase verbinding.")
        return
    
    # --- TIJDELIJKE TEST ---
    huidige_tijd = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    send_push_notification(f"Systeem Test - {huidige_tijd}", "Dit is een controlebericht vanuit de backend om de verbinding te monitoren.")
    # -----------------------

    cookies = get_user_cookies(MY_UID)
    headers = {'User-Agent': 'Mozilla/5.0'}
    notified_meetings = load_notified(NOTIFIED_MEETINGS_FILE)
    new_meetings_notified = False

    datum_grens = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

    try:
        url = f"{DRONTEN_API_V2}/meetings?limit=40&sort=date_desc"
        logging.info("Ophalen vergaderingen lijst via API...")
        # CRUCIALE FIX: Timeout voorkomt deadlock op ORI API
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        meetings = resp.json().get('result', {}).get('meetings', [])
        logging.info(f"{len(meetings)} vergaderingen gevonden in API.")

        for meeting in meetings:
            m_date = meeting.get('date', '')
            if m_date < datum_grens: continue

            m_id = str(meeting['id'])
            
            # --- NIEUWE LOGICA: Controleer eerst of hij al in Firebase staat ---
            # We halen het document op uit de database om te kijken of hij bestaat
            doc_ref = db.collection('vergaderingen').document(m_id)
            doc_snap = doc_ref.get(timeout=10)
            
            if doc_snap.exists:
                doc_data = doc_snap.to_dict()
                needs_update = False
                update_data = {}

                # 1. Controleer op verplaatsingen (datum of tijd gewijzigd)
                if doc_data.get('date') != m_date or doc_data.get('startTime') != meeting.get('startTime'):
                    needs_update = True
                    update_data['date'] = m_date
                    update_data['startTime'] = meeting.get('startTime')
                    logging.info(f"Wijziging: Vergadering {m_id} is verplaatst.")

                # 2. Controleer op wijzigingen in de titel (bijv. toevoeging van "[GEANNULEERD]")
                api_title = meeting.get('title') or "Vergadering"
                if doc_data.get('title') != api_title:
                    needs_update = True
                    update_data['title'] = api_title
                    logging.info(f"Wijziging: Titel van vergadering {m_id} is aangepast.")

                # 3. Controleer op NIEUWE documenten (zonder oude te overschrijven)
                detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
                try:
                    d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=15)
                    if d_resp.status_code == 200:
                        api_items = d_resp.json().get('items', [])
                        
                        # Haal bestaande document ID's op uit Firebase
                        existing_items = doc_data.get('items', [])
                        existing_item_ids = [item.get('id') for item in existing_items if 'id' in item]
                        
                        # Filter alleen de documenten die we nog niet hebben
                        new_items = [item for item in api_items if item.get('id') not in existing_item_ids]
                        
                        if new_items:
                            needs_update = True
                            logging.info(f"Wijziging: {len(new_items)} nieuwe documenten gevonden voor vergadering {m_id}.")
                            
                            # Verwerk ALLEEN de nieuwe items met je bestaande functie
                            _, enriched_new_items = extract_media_enriched(meeting.get('url'), headers, cookies, new_items)
                            
                            # Voeg de nieuwe items samen met de bestaande items
                            update_data['items'] = existing_items + enriched_new_items
                except Exception as e:
                    logging.warning(f"Fout bij ophalen document-details voor bestaande vergadering {m_id}: {e}")

                # 4. Voer de update alleen uit als er daadwerkelijk iets is veranderd
                if needs_update:
                    update_data['last_sync'] = firestore.SERVER_TIMESTAMP
                    # Gebruik .update() in plaats van .set() om alleen specifieke velden te wijzigen
                    doc_ref.update(update_data, timeout=15)
                    logging.info(f"Vergadering {m_id} succesvol bijgewerkt in Firebase.")
                else:
                    logging.info(f"Geen actie nodig. Vergadering {m_id} is up-to-date.")
                
                # Ga door naar de volgende vergadering in de for-loop
                continue            # -------------------------------------------------------------------

            # Vanaf hier komen we alleen als de vergadering nog NIET in de database staat
            logging.info(f"Nieuwe vergadering gevonden ({m_id}). Details ophalen...")
            items_lijst = [] 
            detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
            
            try:
                # CRUCIALE FIX: Timeout bij ophalen details
                d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=15)
                if d_resp.status_code == 200:
                    items_lijst = d_resp.json().get('items', [])
            except Exception as e: 
                logging.warning(f"Fout bij ophalen details voor vergadering {m_id}: {e}")

            m_media, enriched_items = extract_media_enriched(meeting.get('url'), headers, cookies, items_lijst)

            logging.info(f"Schrijven nieuwe vergadering {m_id} naar Firestore...")
            
            # CRUCIALE FIX: Timeout op Firestore schrijfactie
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
            }, merge=True, timeout=15)

            if m_id not in notified_meetings:
                send_push_notification(f"Nieuwe agenda: {meeting.get('title')}", f"Datum: {m_date[:10]}")
                notified_meetings.add(m_id)
                new_meetings_notified = True

        if new_meetings_notified:
            save_notified(NOTIFIED_MEETINGS_FILE, notified_meetings)
            logging.info("Notificatie-cache bijgewerkt.")

    except Exception as e:
        logging.error(f"Monitor Fout in run_monitor(): {e}", exc_info=True)
    finally:
        logging.info("--- Einde ORI Monitor Run ---")

if __name__ == "__main__":
    try:
        run_monitor()
    except Exception as e:
        logging.error(f"Fatale fout bij het starten van de monitor: {e}", exc_info=True)