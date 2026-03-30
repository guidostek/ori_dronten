# -*- coding: utf-8 -*-
import requests
import json
import os
import sys
import fcntl  # Dit is nieuw: helpt ons om een 'slot' op het script te zetten
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

# --- ANTI-OVERLAP SLOTJE (CRUCIALE FIX) ---
# Dit zorgt ervoor dat de cronjob nooit 2 scripts tegelijk kan laten draaien
lock_file_path = '/tmp/monitor_ori.lock'
lock_file = open(lock_file_path, 'w')
try:
    # Probeer het slotje op te eisen exclusief voor dit script
    fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    logging.warning("Script draait al! We stoppen deze overlappende run om loops te voorkomen.")
    sys.exit(0) # Stop onmiddellijk!

logging.info("--- Start Veilige ORI Monitor Run ---")

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
    """Slaat data robuust op."""
    try:
        data_list = list(data_set)
        temp_file = filepath + ".tmp"
        with open(temp_file, 'w') as f:
            json.dump(data_list, f)
            f.flush()
            os.fsync(f.fileno()) 
        os.replace(temp_file, filepath)
    except Exception as e:
        logging.error(f"Fout bij opslaan cache {filepath}: {e}")

# --- NOTIFICATIE LOGICA (IN TEST MODUS!) ---
#def send_push_notification(title, body, doc_id="", link=""):
#    """
#    TEST MODUS: Print alleen in het logboek, stuurt NIETS naar telefoons.
#    Zo voorkomen we spam terwijl we controleren of de loop weg is.
#    """
#    logging.info(f"👉 [TEST-MODUS] Zou nu notificatie sturen: '{title}' voor document: {doc_id}")
    # De daadwerkelijke verzend-code is tijdelijk verwijderd voor de rust.
    
# --- NOTIFICATIE LOGICA (LIVE!) ---
def send_push_notification(title, body, doc_id="", link=""):
    """Verstuurt de pushberichten daadwerkelijk naar de app."""
    try:
        # We maken de tekst veilig om crashes te voorkomen
        safe_title = str(title)[:150]
        safe_body = str(body)[:500]
        
        global db
        if db is None: return

        # Haal alle actieve tokens op uit de Firebase database
        users_ref = db.collection('users').stream()
        tokens = [user.to_dict().get('fcmToken') for user in users_ref if user.to_dict().get('fcmToken')]
                
        if not tokens: return

        # Stuur het bericht naar elke gebruiker
        success_count = 0
        for token in tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(title=safe_title, body=safe_body),
                    data={
                        'trigger': 'open_document', 
                        'document_id': str(doc_id), 
                        'url': str(link) # Hier sturen we de verborgen link mee!
                    },
                    token=token
                )
                messaging.send(message)
                success_count += 1
            except Exception:
                # Als een specifiek token niet meer werkt, negeren we die gewoon
                pass 

        logging.info(f"Notificatie succesvol verzonden naar {success_count} apparaten.")

    except Exception as e:
        logging.error(f"Fout in notificatie-keten: {e}")


# --- SCRAPER FUNCTIE ---
def extract_media_enriched(relative_url, headers, cookies, items_lijst):
    if not relative_url: return [], items_lijst
    full_url = f"{BASE_URL_PUBLIC}{relative_url}"
    meeting_media = []
    try:
        response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200: return meeting_media, items_lijst
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for a in soup.find_all('a', href=re.compile(r'\.mp3$', re.I)):
            href = a.get('href')
            url = href if href.startswith('http') else f"{BASE_URL_PUBLIC}{href}"
            meeting_media.append({'title': 'Volledige audio', 'url': url, 'type': 'audio'})

        for item in items_lijst:
            title = item.get('title', '')
            container = soup.find(lambda t: t.name in ['div', 'li'] and title in t.get_text())
            if container:
                vids = []
                for link in container.find_all('a', href=re.compile(r'\.mp4$', re.I)):
                    vids.append({'title': 'Videofragment', 'url': link.get('href'), 'type': 'video'})
                item['item_media'] = vids
    except Exception as e:
        logging.error(f"Fout tijdens scrapen: {e}")
    return meeting_media, items_lijst

# --- HOOFD PROGRAMMA ---
def run_monitor():
    if db is None: return
    
    cookies = get_user_cookies(MY_UID)
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    notified_meetings = load_notified(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_notified(NOTIFIED_DOCS_FILE)
    
    vandaag_str = datetime.now().strftime('%Y-%m-%d')
    datum_grens = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

    # --- DEEL 1: CHECK LOSSE DOCUMENTEN (/documents) ---
    try:
        doc_api = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
        d_resp = requests.get(doc_api, headers=headers, cookies=cookies, timeout=20)
        
        if d_resp.status_code == 200:
            docs = d_resp.json().get('result', {}).get('documents', [])
            for d in docs:
                d_id = str(d['id'])
                cache_key = f"los_{d_id}"
                
                if cache_key not in notified_docs:
                    notified_docs.add(cache_key)
                    save_notified(NOTIFIED_DOCS_FILE, notified_docs)

                    d_title = str(d.get('description') or d.get('filename') or f"Stuk {d_id}")
                    is_vertrouwelijk = d.get('confidential', False)
                    d_url = f"{DRONTEN_API_V2}/documents/{d_id}/download"
                    
                    d_date_raw = str(d.get('date') or d.get('date_published') or d.get('created_at') or vandaag_str)
                    d_date = d_date_raw[:10]

                    if d_date >= vandaag_str:
                        prefix = "🔒 Besloten: " if is_vertrouwelijk else "Nieuw document: "
                        send_push_notification(f"{prefix}{d_title}", "Tik om document te openen", f"doc_{d_id}", link=d_url)
                    
                    try:
                        db.collection('raadstukken').document(d_id).set({
                            'id': d_id,
                            'title': d_title,
                            'confidential': is_vertrouwelijk,
                            'url': d_url,
                            'timestamp': firestore.SERVER_TIMESTAMP
                        }, merge=True, timeout=5) 
                    except Exception as e:
                        logging.warning(f"Opslag DB los doc {d_id} overgeslagen: {e.__class__.__name__}")

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
                doc_snap = doc_ref.get(timeout=5)
            except Exception as e:
                continue

            if doc_snap.exists:
                doc_data = doc_snap.to_dict()
                detail_url = f"{DRONTEN_API_V1}/meetings/{m_id}"
                
                try:
                    d_resp = requests.get(detail_url, headers=headers, cookies=cookies, timeout=15)
                    if d_resp.status_code == 200:
                        api_items = d_resp.json().get('items', [])
                        existing_ids = [i.get('id') for i in doc_data.get('items', []) if 'id' in i]
                        
                        new_items = []
                        for i in api_items:
                            item_id = str(i.get('id'))
                            cache_key = f"item_{item_id}"
                            
                            if i.get('id') not in existing_ids and cache_key not in notified_docs:
                                new_items.append(i)

                        if new_items:
                            for i in new_items:
                                notified_docs.add(f"item_{i.get('id')}")
                            save_notified(NOTIFIED_DOCS_FILE, notified_docs)
                            
                            first_doc_url = new_items[0].get('resource_url') if new_items[0].get('resource_url') else ""
                            titels = ", ".join([str(i.get('title', 'Document')) for i in new_items])
                            
                            if m_date >= vandaag_str:
                                send_push_notification(f"Nieuwe stukken: {m_title}", f"Toegevoegd: {titels}", m_id, link=first_doc_url)

                            _, enriched = extract_media_enriched(meeting.get('url'), headers, cookies, new_items)
                            try:
                                doc_ref.update({'items': doc_data.get('items', []) + enriched, 'last_sync': firestore.SERVER_TIMESTAMP}, timeout=5)
                            except Exception as fire_err:
                                logging.warning(f"Firestore update voor vergadering {m_id} overgeslagen: {fire_err.__class__.__name__}")
                except Exception as e:
                    logging.warning(f"Detail check mislukt voor {m_id}: {e}")
            
            else:
                if m_id not in notified_meetings:
                    notified_meetings.add(m_id)
                    save_notified(NOTIFIED_MEETINGS_FILE, notified_meetings)
                    
                    if m_date >= vandaag_str:
                        send_push_notification(f"Nieuwe agenda: {m_title}", f"Datum: {m_date[:10]}", m_id)
                
                try:
                    doc_ref.set({'id': int(m_id), 'title': m_title, 'date': m_date, 'last_sync': firestore.SERVER_TIMESTAMP}, merge=True, timeout=5)
                except Exception as e:
                    logging.warning(f"Firestore set mislukt voor {m_id}: {e.__class__.__name__}")

    except Exception as e:
        logging.error(f"Algemene fout in monitor: {e}")
    finally:
        logging.info("--- Einde ORI Monitor Run ---")

if __name__ == "__main__":
    run_monitor()
