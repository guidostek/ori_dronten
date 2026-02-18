import requests
import json
import logging
import os
import sys
from urllib.parse import quote

# --- CONFIGURATIE ---
HA_WEBHOOK_URL = "http://192.168.178.50:8123/api/webhook/ori_dronten_webhook_secret_123"

# TRUE = Pak het nieuwste document, stuur melding, sla NIET op.
# FALSE = Normaal draaien (alleen nieuwe melden + opslaan).
TEST_MODE = False

# De URL zoals jij hem wilde (en die correct is)
DOCUMENTS_API_URL = "https://gemeenteraad.dronten.nl/api/v2/documents?sort=id_desc&limit=20"
DOWNLOAD_BASE_URL = "https://gemeenteraad.dronten.nl/api/v2/documents"

STATE_FILE = "seen_global_docs.json"
LOG_FILE = "global_docs_monitor.log"

# --- LOGGING ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- TRACKING LOGICA ---

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
        logging.error(f"Fout bij opslaan state: {e}")

# --- NOTIFICATIE LOGICA ---

def send_batch_notification(new_documents):
    if not new_documents: return

    display_docs = new_documents[:10]
    remaining = len(new_documents) - 10
    
    doc_list_text = ""
    actions = []

    for idx, doc in enumerate(display_docs):
        doc_id = doc['id']
        # Let op: API gebruikt 'fileName' (camelCase), fallback naar 'filename'
        filename = doc.get('fileName') or doc.get('filename') or 'Naamloos document'
        
        # Links
        direct_dl = f"{DOWNLOAD_BASE_URL}/{doc_id}/download"
        viewer_url = f"https://docs.google.com/viewer?url={quote(direct_dl)}&embedded=true"
        
        doc_list_text += f"- [{filename}]({viewer_url})\n"

        if idx < 3:
            actions.append({
                "action": "URI", 
                "title": f"Open {idx+1}", 
                "uri": viewer_url
            })

    if remaining > 0:
        doc_list_text += f"\n... en nog {remaining} andere documenten."

    first_id = new_documents[0]['id']
    first_dl = f"{DOWNLOAD_BASE_URL}/{first_id}/download"
    first_viewer = f"https://docs.google.com/viewer?url={quote(first_dl)}&embedded=true"

    # Titel aanpassen voor TEST vs PRODUCTIE
    if TEST_MODE:
        title_text = "[TEST] Laatste document Dronten"
        message_prefix = "Dit is een TEST melding (live data):\n\n"
    else:
        title_text = f"{len(new_documents)} Nieuwe documenten Dronten"
        if len(new_documents) == 1: title_text = "Nieuw document: Dronten"
        message_prefix = "Zojuist gepubliceerd:\n\n"

    payload = {
        "title": title_text,
        "message": f"{message_prefix}{doc_list_text}",
        "data": {
            "clickAction": first_viewer, 
            "url": first_viewer,        
            "actions": actions,
            "channel": "Raadsinformatie",
            "tag": "global-docs-dronten",
            "importance": "high",
            "priority": "high"
        }
    }
    
    try:
        requests.post(HA_WEBHOOK_URL, json=payload, timeout=10)
        logging.info(f"Notificatie verstuurd ({len(new_documents)} docs).")
    except Exception as e:
        logging.error(f"Fout bij versturen webhook: {e}")

# --- MONITOR LOGICA ---

def check_documents():
    logging.info("Start controle...")
    
    try:
        # 1. API AANROEPEN
        resp = requests.get(DOCUMENTS_API_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        
        if resp.status_code != 200:
            logging.error(f"API Fout: {resp.status_code}")
            return

        data = resp.json()
        items = []

        # 2. JSON PARSEN (De Correctie!)
        # We kijken nu in ['result']['documents'] zoals in jouw log te zien was
        if 'result' in data and 'documents' in data['result']:
            items = data['result']['documents']
        elif 'documents' in data:
            items = data['documents']
        elif 'items' in data:
            items = data['items']
        elif isinstance(data, list):
            items = data
            
        logging.info(f"Aantal items gevonden: {len(items)}")

        if not items:
            logging.warning("Geen documenten gevonden in de JSON structuur.")
            return

        # 3. TEST MODUS LOGICA
        if TEST_MODE:
            logging.info("--- TEST MODUS ACTIEF ---")
            latest_doc = items[0] # Pak de bovenste (nieuwste vanwege sort=id_desc)
            logging.info(f"Test document: {latest_doc.get('fileName')}")
            
            send_batch_notification([latest_doc])
            logging.info("Test voltooid. Script stopt.")
            return

        # 4. PRODUCTIE LOGICA
        seen_ids = load_seen_docs()
        new_batch = []
        
        for doc in items:
            doc_id = str(doc['id'])
            if doc_id not in seen_ids:
                new_batch.append(doc)
                seen_ids.add(doc_id)
        
        if new_batch:
            logging.info(f"{len(new_batch)} nieuwe documenten.")
            send_batch_notification(new_batch)
            save_seen_docs(seen_ids)
        else:
            logging.info("Geen nieuwe documenten.")

    except Exception as e:
        logging.error(f"Fout tijdens controle: {e}")

if __name__ == "__main__":
    check_documents()
