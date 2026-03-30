# -*- coding: utf-8 -*-
import requests
import firebase_admin
from firebase_admin import credentials, firestore
import logging
import sys

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
console = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(console)

# --- FIREBASE INITIALISATIE ---
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(CRED_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Succesvol verbonden met Firebase.")
except Exception as e:
    logging.error(f"Fout bij het verbinden met Firebase: {e}")
    sys.exit(1)

def sync_naar_firestore(endpoint, collectie_naam):
    """
    Haalt data op van een specifiek API endpoint en slaat dit op in Firestore.
    """
    url = f"{DRONTEN_API_V2}/{endpoint}"
    logging.info(f"Ophalen van {endpoint} via {url}...")

    try:
        # User-Agent is toegevoegd, omdat sommige overheidsservers requests anders blokkeren
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)

        if response.status_code != 200:
            logging.error(f"Fout bij ophalen {endpoint}: Status {response.status_code}")
            return

        data = response.json()

        # Robuust de items uit de JSON halen (zoals we ook bij de documenten doen)
        items = []
        if 'result' in data:
            if isinstance(data['result'], list):
                items = data['result']
            elif isinstance(data['result'], dict):
                # Als het genest is, probeer de endpoint naam te gebruiken (bijv. 'groups' of 'roles')
                items = data['result'].get(endpoint) or data['result'].get('items') or []
        elif 'items' in data:
            items = data['items']
        elif isinstance(data, list):
            items = data

        if not items:
            logging.warning(f"Geen data gevonden om te synchroniseren voor '{endpoint}'. Controleer de API-structuur.")
            return

        logging.info(f"{len(items)} items gevonden voor '{endpoint}'. Start opslaan naar collectie '{collectie_naam}'...")

        # We gebruiken een batch voor efficiënt wegschrijven
        batch = db.batch()
        aantal = 0

        for item in items:
            # Gebruik het originele API ID als document ID, fallback naar een teller als er geen ID is
            item_id = str(item.get('id', item.get('uuid', aantal))) 
            doc_ref = db.collection(collectie_naam).document(item_id)

            # merge=True zorgt dat we bestaande velden updaten, maar handmatige toevoegingen in Firestore niet overschrijven
            batch.set(doc_ref, item, merge=True) 
            aantal += 1

            # Firestore batches hebben een limiet van 500 handelingen, dus we committen per 400
            if aantal % 400 == 0:
                batch.commit()
                batch = db.batch()

        # Commit de resterende items
        if aantal % 400 != 0:
            batch.commit()

        logging.info(f"✅ Succesvol {aantal} records gesynchroniseerd naar Firestore collectie: '{collectie_naam}'.")

    except Exception as e:
        logging.error(f"Fout tijdens het verwerken van {endpoint}: {e}")

if __name__ == "__main__":
    logging.info("--- Start Synchronisatie Groepen & Rollen ---")

    # 1. Sync Groepen (Fracties, commissies, etc.)
    sync_naar_firestore('groups', 'groepen')

    # 2. Sync Rollen (Raadslid, voorzitter, griffier, etc.)
    sync_naar_firestore('roles', 'rollen')

    logging.info("--- Synchronisatie Voltooid ---")
