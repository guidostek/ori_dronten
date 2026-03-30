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

# --- NOTIFICATIE LOGICA ---

def send_item_notification(item_title, documents, meeting_data):
    if not documents: return
    base_url = meeting_data.get('fullUrl')
    if base_url:
        full_agenda_url = f"https://gemeenteraad.dronten.nl{base_url}/{slugify(item_title)}" if base_url.startswith('/') else f"{base_url}/{slugify(item_title)}"
    else: full_agenda_url = "https://gemeenteraad.dronten.nl"

    doc_list_text = ""
    actions = []
    for idx, doc in enumerate(documents):
        doc_id = doc['id']
        dl = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
        vw = f"https://docs.google.com/viewer?url={quote(dl)}&embedded=true"
        doc_list_text += f"{idx + 1}. {doc['filename']}\n[Bekijken]({vw})\n\n"
        if idx < 3: actions.append({"action": "URI", "title": f"Open PDF {idx + 1}", "uri": vw})

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
    requests.post(HA_WEBHOOK_URL, json=payload, timeout=10)
    logging.info(f"Notificatie verstuurd: {item_title}")

# --- MONITOR LOGICA ---

def run_monitor():
    logging.info("--- Start HA Monitor (Venster 6 weken) ---")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: seen_ids = set(json.load(f))
    else: seen_ids = set()
    
    now = datetime.now()
    dt_from = now - timedelta(days=14)
    dt_to = now + timedelta(days=30)
    
    date_str = dt_from.strftime('%Y-%m-%d')
    url = f"{DRONTEN_API_V2}/meetings?sort=id_desc&date_from={date_str}&limit=100"
    
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        if resp.status_code != 200: return
        data = resp.json()
        
        # Robuuste check op resultaat-key
        res = data.get('result', {})
        items = []
        if isinstance(res, dict):
            items = res.get('meetings') or res.get('items') or []
        if not items and 'items' in data: items = data['items']
        if not items and 'meetings' in data: items = data['meetings']
        
        found_new = False
        for meta in items:
            m_date_str = meta.get('date', '')
            if not m_date_str: continue
            m_date = datetime.strptime(m_date_str[:10], '%Y-%m-%d')
            
            if not (dt_from <= m_date <= dt_to): continue

            detail_url = f"{DRONTEN_API_V1}/meetings/{meta['id']}"
            d_resp = requests.get(detail_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            if d_resp.status_code != 200: continue
            meeting_data = d_resp.json()
            
            for item in (meeting_data.get('items') or []):
                new_docs = [d for d in item.get('documents', []) if str(d['id']) not in seen_ids]
                if new_docs:
                    send_item_notification(item.get('title', 'Agendapunt'), new_docs, meeting_data)
                    for d in new_docs: seen_ids.add(str(d['id']))
                    found_new = True
                    if TEST_MODE: break
            if TEST_MODE and found_new: break

        if found_new:
            with open(STATE_FILE, 'w') as f: json.dump(list(seen_ids), f)
    except Exception as e:
        logging.error(f"Fout: {e}")

if __name__ == "__main__":
    run_monitor()
