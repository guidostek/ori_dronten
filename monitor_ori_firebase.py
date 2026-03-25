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

# --- MAPS & PADEN ---
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

logging.info("--- Start Gecorrigeerde ORI Monitor Run ---")

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
        try:
            with open(session_path, 'r') as f:
                return json.load(f).get('cookies', {})
        except: return None
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

# --- NOTIFICATIE LOGICA ---
def send_push_notification(title, body, doc_id="", link=""):
    """Verstuurt pushberichten met een directe link naar het document."""
    try:
        logging.info(f"Aanroepen FCM voor {doc_id}. Link aanwezig: {bool(link)}")
        
        # Voorkom crashes door datatypes te forceren naar strings en in te korten
        safe_title = str(title)[:100]
        safe_body = str(body)[:500]
        
        global db
        if db is None:
            logging.error("FCM afgebroken: Geen DB verbinding.")
            return

        # Haal tokens op uit de 'users' collectie
        users_ref = db.collection('users').stream()
        tokens = []
        for user in users_ref:
            token = user.to_dict().get('fcmToken')
            if token:
                tokens.append(token)
                
        if not tokens:
            logging.warning("Geen actieve FCM tokens gevonden in database.")
            return

        # Verstuur berichten
        success_count = 0
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=safe_title, body=safe_body),
                    data={
                        'trigger': 'open_document', 
                        'document_id': str(doc_id),
                        'url': str(link)
                    },
                    token=token
                )
                messaging.send(message)
                success_count += 1
            except Exception as e:
                logging.debug(f"Kon niet sturen naar token {token}: {e}")

        logging.info(f"Notificatie succesvol verzonden naar {success_count} apparaten.")

    except Exception as e:
        logging.error(f"Fout in de notificatie-keten: {e}", exc_info=True)

# --- SCRAPER FUNCTIE ---
def extract_media_enriched(relative_url, headers, cookies, items_lijst):
    """Scrapt audio en video fragmenten van de publieke pagina."""
    if not relative_url:
        return [], items_lijst
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    try:
        response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200:
            return meeting_media, items_lijst
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Audio check
        for a in soup.find_all('a', href=re.compile(r'\.mp3$', re.I)):
            href = a.get('href')
            url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
            meeting_media.append({'title': 'Volledige audio', 'url': url, 'type': 'audio'})

        # Items verrijken met video
        for item in items_lijst:
            title = item.get('title', '')
            container = soup.find(lambda t: t.name in ['div', 'li'] and title in t.get_text())
            if container:
                vids = []
                for link in container.find_all('a', href=re.compile(r'\.mp4$', re.I)):
                    vids.append({'title': 'Videofragment', 'url': link.get('href'), 'type': 'video'})
                item['item_media'] = vids
    except Exception as e:
        logging.error(f"Fout tijdens scrapen van {full_url}: {e}")
    return meeting_media, items_lijst

# --- HOOFD PROGRAMMA ---
def run_monitor():
    if db is None:
        logging.error("Monitor gestopt: Geen Firebase.")
        return
    
    cookies = get_user_cookies(MY_UID)
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Laad lokale cache bestanden
    notified_meetings = load_notified(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_notified(NOTIFIED_DOCS_FILE)
    
    datum_grens = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    cache_gewijzigd = False # Houdt bij of we de json bestanden moeten opslaan

    # --- DEEL 1: CHECK LOSSE DOCUMENTEN (/documents) ---
    try:
        logging.info("Checken op losse documenten...")
        doc_api = f"{DRONTEN_API_V2}/documents?limit=20&sort=date_desc"
        d_resp = requests.get(doc_api, headers=headers, cookies=cookies, timeout=20)
        
        if d_resp.status_code == 200:
            docs = d_resp.json().get('result', {}).get('documents', [])
            for d in docs:
                d_id = str(d['id'])
                cache_key = f"los_{d_id}" # Unieke sleutel voor losse documenten
                
                if cache_key not in notified_docs:
                    d_title = str(d.get('title') or "Nieuw document")
                    d_url = d.get('url_public') or ""
                    
                    send_push_notification("Nieuw document beschikbaar", d_title, f"doc_{d_id}", link=d_url)
                    
                    notified_docs.add(cache_key)
                    cache_gewijzigd = True
    except Exception as e:
        logging.error(f"Fout bij losse documenten check: {e}")

    # --- DEEL 2: CHECK VERGADERINGEN EN GESTRUCTUREERDE DOCS ---
    try:
        url = f"{DRONTEN_API_V2}/meetings?limit=40&sort=date_desc"
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        meetings = resp.json().get('result', {}).get('meetings', [])

        for meeting in meetings:
            m_date = meeting.get('date', '')
            if m_date < datum_grens: continue
            
            m_id = str(meeting['id'])
            m_title = str(meeting.get('title') or "Vergadering")
            doc_ref = db.collection('vergaderingen').document(m_id)
            
            try:
                doc_snap = doc_ref.get(timeout=10)
            except Exception as e:
                logging.error(f"Kon Firestore niet lezen voor {m_id} (Quota?): {e}")
                continue

            if doc_snap.exists:
                # Bestaande vergadering: Zoek naar nieuwe documenten
                doc_data = doc_snap.to_dict()
                detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
                
                try:
                    d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=15)
                    if d_resp.status_code == 200:
                        api_items = d_resp.json().get('items', [])
                        existing_ids = [i.get('id') for i in doc_data.get('items', []) if 'id' in i]
                        
                        # Check ook in onze lokale notified_docs lijst!
                        new_items = []
                        for i in api_items:
                            item_id = str(i.get('id'))
                            cache_key = f"item_{item_id}"
                            
                            # Alleen toevoegen als hij niet in firebase staat EN we nog geen notificatie hebben gestuurd
                            if i.get('id') not in existing_ids and cache_key not in notified_docs:
                                new_items.append(i)

                        if new_items:
                            logging.info(f"Wijziging: {len(new_items)} nieuwe documenten voor {m_id}")
                            
                            first_doc_url = new_items[0].get('resource_url') if new_items[0].get('resource_url') else ""
                            titels = ", ".join([str(i.get('title', 'Document')) for i in new_items])[:150]
                            
                            # Stuur notificatie (Eerst!)
                            send_push_notification(f"Nieuwe stukken: {m_title}", f"Toegevoegd: {titels}", m_id, link=first_doc_url)

                            # Sla lokaal op dat we deze hebben gemeld
                            for i in new_items:
                                item_id = str(i.get('id'))
                                notified_docs.add(f"item_{item_id}")
                            cache_gewijzigd = True

                            # Verwerking: Probeer Firebase bij te werken (Daarna pas!)
                            _, enriched = extract_media_enriched(meeting.get('url'), headers, cookies, new_items)
                            try:
                                doc_ref.update({
                                    'items': doc_data.get('items', []) + enriched,
                                    'last_sync': firestore.SERVER_TIMESTAMP
                                }, timeout=15)
                            except Exception as fire_err:
                                logging.error(f"Firestore update mislukt voor {m_id}: {fire_err}. Notificatie is wel lokaal gemarkeerd.")
                except Exception as e:
                    logging.warning(f"Detail check mislukt voor {m_id}: {e}")
            
            else:
                # VOLLEDIG NIEUWE VERGADERING
                if m_id not in notified_meetings:
                    logging.info(f"Nieuwe agenda ontdekt: {m_id}")
                    send_push_notification(f"Nieuwe agenda: {m_title}", f"Datum: {m_date[:10]}", m_id)
                    notified_meetings.add(m_id)
                    cache_gewijzigd = True
                
                # Probeer op te slaan in Firebase
                try:
                    doc_ref.set({
                        'id': int(m_id),
                        'title': m_title,
                        'date': m_date,
                        'last_sync': firestore.SERVER_TIMESTAMP
                    }, merge=True, timeout=15)
                except Exception as e:
                    logging.error(f"Firestore set mislukt voor nieuwe vergadering {m_id}: {e}")

        # Sla de lokale cache bestanden op als er iets is toegevoegd
        if cache_gewijzigd:
            save_notified(NOTIFIED_DOCS_FILE, notified_docs)
            save_notified(NOTIFIED_MEETINGS_FILE, notified_meetings)
            logging.info("Lokale notificatie-caches succesvol opgeslagen.")

    except Exception as e:
        logging.error(f"Algemene fout in monitor: {e}", exc_info=True)
    finally:
        logging.info("--- Einde ORI Monitor Run ---")

if __name__ == "__main__":
    run_monitor()
