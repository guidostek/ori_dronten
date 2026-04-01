# -*- coding: utf-8 -*-
import requests
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore
from config import *
from firebase_client import db
from notification_service import send_push_notification

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "sync_orchestrator.log")),
        logging.StreamHandler()
    ]
)

def load_cache(filepath):
    """Laadt de lijst met reeds genotificeerde ID's."""
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def save_cache(filepath, data_set):
    """Slaat de actuele lijst met genotificeerde ID's op."""
    with open(filepath, 'w') as f:
        json.dump(list(data_set), f)

def get_sync_start_date_str():
    """Berekent de startdatum (3 maanden terug) en geeft dit in YYYY-MM-DD formaat."""
    start_date = datetime.now(timezone.utc) - timedelta(days=DEEP_SYNC_DAYS)
    return start_date.strftime('%Y-%m-%d')

def extract_api_data(response_json, endpoint_naam="API"):
    """
    Extraheert veilig de data array uit de API v2 response met uitgebreide logging.
    """
    if not isinstance(response_json, dict):
        return response_json
        
    result = response_json.get("result", response_json)
    
    if isinstance(result, dict):
        if "model" in result:
            return result["model"]
        elif "meetingitems" in result:
            return result["meetingitems"]
        elif "meetings" in result:
            return result["meetings"]
        elif "documents" in result:
            return result["documents"]
        elif "items" in result:
            return result["items"]
        else:
            logging.warning(f"ONBEKENDE STRUCTUUR bij {endpoint_naam}. Gevonden keys: {list(result.keys())}")
            return []
            
    if isinstance(result, list):
        return result
        
    return []

def run_deep_sync():
    """Voert de exacte trapsgewijze deep sync uit volgens de API V2 documentatie."""
    date_from_str = get_sync_start_date_str()
    logging.info(f"Start Deep Sync vanaf datum: {date_from_str}")

    notified_meetings = load_cache(NOTIFIED_MEETINGS_FILE)
    notified_docs = load_cache(NOTIFIED_DOCS_FILE)
    
    new_meetings_found = False
    new_docs_found = False

    # STAP 1: MEETINGS OPHALEN
    meetings_url = f"{DRONTEN_API_V2}/meetings?sort=id_desc&limit=50"
    logging.info(f"Aanroepen API voor meetings: {meetings_url}")
    
    try:
        resp = requests.get(meetings_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logging.error(f"API weigerde toegang tot meetings: HTTP {resp.status_code}")
            return

        meetings = extract_api_data(resp.json(), endpoint_naam=meetings_url)
        logging.info(f"Totaal aantal meetings gevonden in API: {len(meetings)}")

        for meeting in meetings:
            m_id = str(meeting.get('id'))
            if not m_id or m_id == 'None':
                continue
                
            m_date = meeting.get('date', '')
            title = meeting.get('title') or "Vergadering"
            
            logging.info(f"--- Start verwerking Meeting ID: {m_id} ({title}) ---")

            # DEBUG LOGGING VOOR TEST VERGADERINGEN
            if m_id in ['1467', '1468']:
                logging.info(f"DEBUG MEETING {m_id} JSON: {json.dumps(meeting)}")

            # 1.1 Controleer en Schrijf Meeting weg naar Firestore
            new_meeting_data = {
                'id': int(m_id),
                'title': title,
                'date': m_date,
                'startTime': meeting.get('startTime', ''),
                'confidential': bool(meeting.get('confidential', 0)),
                'url_public': f"{BASE_URL_PUBLIC}{meeting.get('url')}" if meeting.get('url') else "",
                'location': meeting.get('location', '')
            }

            try:
                doc_ref = db.collection('vergaderingen').document(m_id)
                doc = doc_ref.get()
                needs_update = True

                # Controleer of we bestaande data hebben en vergelijk
                if doc.exists:
                    existing_data = doc.to_dict()
                    if all(existing_data.get(k) == v for k, v in new_meeting_data.items()):
                        needs_update = False
                
                if needs_update:
                    new_meeting_data['last_sync'] = firestore.SERVER_TIMESTAMP
                    doc_ref.set(new_meeting_data, merge=True, timeout=15)
                    logging.info(f"Meeting {m_id} succesvol bijgewerkt in Firestore.")
                else:
                    logging.info(f"Meeting {m_id} is ongewijzigd. (Database overgeslagen)")
            except Exception as e:
                logging.error(f"Firestore fout bij wegschrijven meeting {m_id}: {e}")

            # 1.2 Notificatie voor nieuwe meeting
            if m_id not in notified_meetings:
                send_push_notification(f"Nieuwe agenda: {title}", f"Datum: {m_date[:10]}")
                notified_meetings.add(m_id)
                new_meetings_found = True

            # STAP 2: MEETING ITEMS OPHALEN PER MEETING
            items_url = f"{DRONTEN_API_V2}/meetings/{m_id}/meetingitems"
            
            try:
                items_resp = requests.get(items_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
                if items_resp.status_code == 200:
                    raw_items_json = items_resp.json()

                    # DEBUG LOGGING VOOR TEST VERGADERINGEN
                    if m_id in ['1467', '1468']:
                        logging.info(f"DEBUG ITEMS MEETING {m_id} JSON: {json.dumps(raw_items_json)}")

                    items = extract_api_data(raw_items_json, endpoint_naam=items_url)
                    logging.info(f"Aantal agendapunten gevonden voor meeting {m_id}: {len(items)}")
                    
                    for item in items:
                        item_id = str(item.get('id'))
                        if not item_id or item_id == 'None':
                            continue
                        
                        # 2.1 Controleer en Schrijf Agendapunt (Meeting Item)
                        item_title = item.get('title') or item.get('description') or f"Agendapunt {item_id}"
                        new_item_data = {
                            'id': int(item_id),
                            'meeting_id': int(m_id),
                            'title': item_title
                        }

                        try:
                            item_ref = db.collection('agendapunten').document(item_id)
                            item_doc = item_ref.get()
                            needs_update = True

                            if item_doc.exists:
                                existing_item = item_doc.to_dict()
                                if all(existing_item.get(k) == v for k, v in new_item_data.items()):
                                    needs_update = False

                            if needs_update:
                                new_item_data['last_sync'] = firestore.SERVER_TIMESTAMP
                                item_ref.set(new_item_data, merge=True, timeout=15)
                                logging.info(f"   -> Agendapunt {item_id} weggeschreven naar Firestore.")
                            else:
                                logging.info(f"   -> Agendapunt {item_id} is ongewijzigd.")
                        except Exception as e:
                            logging.error(f"Firestore fout bij wegschrijven agendapunt {item_id}: {e}")

                        # STAP 3: DOCUMENTEN OPHALEN PER MEETING ITEM
                        docs_url = f"{DRONTEN_API_V2}/meetingitems/{item_id}/documents"
                        
                        try:
                            docs_resp = requests.get(docs_url, headers={'User-Agent': 'DrontenRaadApp-Monitor/2.0'}, timeout=TIMEOUT_SECONDS)
                            if docs_resp.status_code == 200:
                                raw_docs_json = docs_resp.json()
                                docs = extract_api_data(raw_docs_json, endpoint_naam=docs_url)
                                
                                if len(docs) > 0:
                                    logging.info(f"     -> {len(docs)} document(en) gevonden voor agendapunt {item_id}.")
                                else:
                                    logging.info(f"     -> Geen documenten voor agendapunt {item_id}.")
                                
                                for doc in docs:
                                    doc_id = str(doc.get('id'))
                                    if not doc_id or doc_id == 'None':
                                        continue
                                        
                                    doc_title = doc.get('fileName') or doc.get('description') or f"Stuk {doc_id}"
                                    download_url = f"{DRONTEN_API_V2}/documents/{doc_id}/download"
                                    
                                    # 4.1 Controleer en Schrijf Document
                                    new_doc_data = {
                                        'id': int(doc_id),
                                        'title': doc_title,
                                        'meeting_id': int(m_id),
                                        'item_id': int(item_id),
                                        'confidential': bool(doc.get('confidential', 0)),
                                        'url': download_url
                                    }

                                    try:
                                        doc_ref = db.collection('raadstukken').document(doc_id)
                                        doc_snap = doc_ref.get()
                                        needs_update = True

                                        if doc_snap.exists:
                                            existing_doc = doc_snap.to_dict()
                                            if all(existing_doc.get(k) == v for k, v in new_doc_data.items()):
                                                needs_update = False

                                        if needs_update:
                                            new_doc_data['last_sync'] = firestore.SERVER_TIMESTAMP
                                            doc_ref.set(new_doc_data, merge=True, timeout=15)
                                            logging.info(f"       -> Document {doc_id} weggeschreven/bijgewerkt.")
                                        # Als het document ongewijzigd is, printen we niets om een gigantische log te voorkomen!
                                            
                                    except Exception as e:
                                        logging.error(f"Firestore fout bij wegschrijven document {doc_id}: {e}")

                                    # 4.2 Notificatie voor nieuw document
                                    if doc_id not in notified_docs and needs_update:
                                        send_push_notification("Nieuw raadsstuk gepubliceerd", doc_title)
                                        notified_docs.add(doc_id)
                                        new_docs_found = True
                            else:
                                logging.error(f"API weigerde toegang tot docs voor item {item_id}: HTTP {docs_resp.status_code}")
                        except Exception as e:
                             logging.error(f"Fout bij ophalen docs voor item {item_id}: {e}")
                else:
                    logging.error(f"API weigerde toegang tot items voor meeting {m_id}: HTTP {items_resp.status_code}")
            except Exception as e:
                logging.error(f"Fout bij ophalen items voor meeting {m_id}: {e}")

    except Exception as e:
        logging.error(f"Fout in run_deep_sync: {e}")

    # Caches opslaan indien gewijzigd
    if new_meetings_found:
        save_cache(NOTIFIED_MEETINGS_FILE, notified_meetings)
        logging.info("Meeting cache bijgewerkt.")
    if new_docs_found:
        save_cache(NOTIFIED_DOCS_FILE, notified_docs)
        logging.info("Document cache bijgewerkt.")

if __name__ == "__main__":
    logging.info("--- Start Sync Orchestrator ---")
    run_deep_sync()
    logging.info("--- Eind Sync Orchestrator ---")