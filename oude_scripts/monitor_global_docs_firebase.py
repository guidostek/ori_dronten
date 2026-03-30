import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore

CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
MY_UID = "Jt7bZksq20QJg3KBPHmm3ij518k1"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

def run_docs_sync():
    session_path = os.path.join(SESSION_DIR, f"{MY_UID}.json")
    cookies = None
    if os.path.exists(session_path):
        with open(session_path, 'r') as f:
            cookies = json.load(f).get('cookies')

    url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
    print(f"--- DEBUG: Documenten ophalen via {url} ---")
    
    try:
        # We voegen de Origin en Referer toe om de browser na te bootsen
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://gemeenteraad.dronten.nl/mijnoverzicht/',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        print(f"--- DEBUG: Status: {resp.status_code} ---")
        
        data = resp.json()
        docs = data.get('result', {}).get('documents', [])
        
        print(f"--- DEBUG: Totaal {len(docs)} documenten in resultaat ---")

        for doc in docs:
            is_geheim = doc.get('confidential', False)
            if is_geheim:
                print(f"GELUKT: Besloten stuk gevonden: {doc.get('description')}")

            doc_id = str(doc['id'])
            db.collection('raadstukken').document(doc_id).set({
                'id': doc_id,
                'title': doc.get('description') or doc.get('filename'),
                'confidential': is_geheim,
                'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
                'timestamp': firestore.SERVER_TIMESTAMP
            }, merge=True)

    except Exception as e:
        print(f"Fout: {e}")

if __name__ == "__main__":
    run_docs_sync()