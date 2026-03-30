import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
MY_UID = "Jt7bZksq20QJg3KBPHmm3ij518k1"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

def get_user_cookies(uid):
    session_path = os.path.join(SESSION_DIR, f"{uid}.json")
    if os.path.exists(session_path):
        with open(session_path, 'r') as f:
            return json.load(f).get('cookies')
    return None

def run_docs_sync():
    cookies = get_user_cookies(MY_UID)
    url = f"{DRONTEN_API_V2}/documents?sort=id_desc&limit=50"
    
    try:
        # De cookies zorgen ervoor dat de API ook de 'besloten' documenten teruggeeft
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, cookies=cookies, timeout=20)
        if resp.status_code != 200: return
        
        docs = resp.json().get('result', {}).get('documents') or []
        
        for doc in docs:
            doc_id = str(doc['id'])
            is_vertrouwelijk = doc.get('confidential', False)
            
            title = doc.get('description') or doc.get('filename') or f"Stuk {doc_id}"
            
            doc_ref = db.collection('raadstukken').document(doc_id)
            doc_ref.set({
                'id': doc_id,
                'title': title,
                'confidential': is_vertrouwelijk,
                'url': f"{DRONTEN_API_V2}/documents/{doc_id}/download",
                'timestamp': firestore.SERVER_TIMESTAMP
            }, merge=True)
            
            if is_vertrouwelijk:
                print(f"Besloten stuk gevonden en gesync: {title}")

    except Exception as e:
        print(f"Docs sync fout: {e}")

if __name__ == "__main__":
    run_docs_sync()
