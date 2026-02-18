import requests
import json
import logging
import os
import sys
import re
from urllib.parse import quote
from datetime import datetime, timedelta

# --- CONFIGURATIE ---
HA_WEBHOOK_URL = "http://192.168.178.50:8123/api/webhook/ori_dronten_webhook_secret_123"
DRONTEN_API_V1 = "https://gemeenteraad.dronten.nl/api/v1"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

STATE_FILE = "seen_docs.json"
LOG_FILE = "ori_monitor.log"

# TEST_MODE: True = stuur direct een melding van de eerste meeting in het venster
TEST_MODE = False

# --- LOGGING ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- HELPER FUNCTIES ---

def slugify(text):
    if not text: return "Agendapunt"
    text = re.sub(r'[^a-zA-Z0-9]', '-', text)
    slug = re.sub(r'-+', '-', text)
    return slug.strip('-')

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

# --- NOTIFICATIE LOGICA ---

def send_item_notification(item_title, documents, meeting_data):
    if not documents: return

    base_url = meeting_data.get('fullUrl')
    if base_url:
        if base_url.startswith('/'):
            full_agenda_url = f"https://gemeenteraad.dronten.nl{base_url}/{slugify(item_title)}"
        else:
            full_agenda_url = f"{base_url}/{slugify(item_title)}"
    else:
        full_agenda_url = "https://gemeenteraad.dronten.nl"

    doc_list_text = ""
    actions = []

    for idx, doc in enumerate(documents):
        doc_id = doc['id']
        direct_dl = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
        viewer_url = f"https://docs.google.com/viewer?url={quote(direct_dl)}&embedded=true"
        
        doc_list_text += f"{idx + 1}. {doc['filename']}\n[Bekijken]({viewer_url})\n\n"

        if idx < 3:
            actions.append({"action": "URI", "title": f"Open PDF {idx + 1}", "uri": viewer_url})

    payload = {
        "title": f"{item_title}",
        "message": f"Nieuwe documenten:\n\n{doc_list_text}",
        "data": {
            "clickAction": full_agenda_url,
            "url": full_agenda_url,        
            "actions": actions,
            "channel": "Raadsinformatie",
            "tag": f"agenda-{item_title}",
            "importance": "high",
            "priority": "high"
        }
    }
    
    try:
        requests.post(HA_WEBHOOK_URL, json=payload, timeout=10)
        logging.info(f"Notificatie verstuurd: {item_title}")
    except Exception as e:
        logging.error(f"Fout bij versturen webhook: {e}")

# --- MONITOR LOGICA ---

def get_meetings_window():
    """Haalt meetings op van 14 dagen geleden tot 30 dagen in de toekomst."""
    now = datetime.now()
    date_from = (now - timedelta(days=14)).strftime('%Y-%m-%d')
    date_to = (now + timedelta(days=30)).strftime('%Y-%m-%d')
    
    # We sorteren op datum zodat we een logische volgorde hebben
    url = f"{DRONTEN_API_V2}/meetings/?sort=date&date_from={date_from}"
    
    try:
        logging.info(f"Venster: {date_from} t/m {date_to}")
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            items = []
            if isinstance(data, dict):
                items = data.get('items') or data.get('result', {}).get('items', [])
            elif isinstance(data, list):
                items = data
            
            # Filter handmatig op de 'date_to' omdat de API dat soms negeert
            filtered = [m for m in items if m.get('date', '') <= f"{date_to} 23:59:59"]
            logging.info(f"{len(filtered)} vergaderingen gevonden in venster.")
            return filtered
    except Exception as e:
        logging.error(f"Fout bij ophalen venster: {e}")
    return []

def run_monitor():
    logging.info("--- Start HA Monitor (Venster 6 weken) ---")
    seen_ids = load_seen_docs()
    total_new_found = False

    meetings = get_meetings_window()
    
    for meta in meetings:
        m_id = str(meta['id'])
        url = f"{DRONTEN_API_V1}/meetings/{m_id}"
        
        try:
            resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            if resp.status_code != 200: continue

            data = resp.json()
            agenda_items = data.get('items') or data.get('agenda_items', [])
            
            for item in agenda_items:
                docs = item.get('documents', [])
                new_docs = [d for d in docs if str(d['id']) not in seen_ids]
                
                if new_docs:
                    title = item.get('title') or item.get('description') or 'Agendapunt'
                    send_item_notification(title, new_docs, data)
                    for d in new_docs: seen_ids.add(str(d['id']))
                    total_new_found = True
                    
                    if TEST_MODE:
                        logging.info("TEST_MODE: Stop na eerste resultaat.")
                        save_seen_docs(seen_ids)
                        return

        except Exception as e:
            logging.error(f"Fout bij meeting {m_id}: {e}")

    if total_new_found:
        save_seen_docs(seen_ids)

if __name__ == "__main__":
    run_monitor()
