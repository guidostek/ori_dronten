import os
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIGURATIE ---
FIREBASE_CRED = "/home/guido/oriscript/serviceAccountKey.json"
GOOGLE_CRED = "/home/guido/oriscript/google_agenda_key.json"
EMAILS_TO_SHARE = ["jouw-eigen-email@gmail.com", "secretariaat@vvddronten.nl"]
LOGO_URL = "https://guidostek.nl/logovvd.png"

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred)
db = firestore.client()

def get_next_tuesday():
    today = datetime.now()
    days_ahead = (1 - today.weekday() + 7) % 7
    return today + timedelta(days=days_ahead)

def get_meeting_data():
    docs = db.collection('vergaderingen') \
             .where(filter=firestore.FieldFilter('synced', '==', True)) \
             .order_by('date', direction=firestore.Query.ASCENDING) \
             .limit(15).get()
    
    skip_keywords = ["opening", "sluiting", "vaststellen", "verslag vorige", "notulen", "mededelingen"]
    keep_keywords = ["raadsvoorstel", "akkoordstuk", "bespreekstuk", "motie", "lta", "brief", "c-brief", "initiatiefvoorstel"]

    sections = {"Raadsvergadering": [], "Oordeelsvormend": [], "Beeldvormend": []}

    for doc in docs:
        data = doc.to_dict()
        m_type = data.get('type', '')
        category = None
        if "Raad" in m_type: category = "Raadsvergadering"
        elif "Oordeel" in m_type: category = "Oordeelsvormend"
        elif "Beeld" in m_type: category = "Beeldvormend"
        
        if category and not sections[category]:
            for item in data.get('items', []):
                title = item.get('title', '')
                t_low = title.lower()
                if any(k in t_low for k in keep_keywords) and not any(s in t_low for s in skip_keywords):
                    sections[category].append(title)
    return sections

def create_google_doc():
    scopes = ['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_file(GOOGLE_CRED, scopes=scopes)
    docs_service = build('docs', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)

    meeting_date = get_next_tuesday().strftime('%d-%m-%Y')
    sections = get_meeting_data()

    # 1. Maak Document
    doc = docs_service.documents().create(body={'title': f"Agenda fractie - {meeting_date}"}).execute()
    doc_id = doc.get('documentId')

    # 2. Inhoud opbouwen
    full_text = f"\n\nAgenda fractievergadering VVD Dronten\n\n"
    full_text += f"● Datum: dinsdag {meeting_date}\n"
    full_text += f"● Tijd: 19:30 uur\n"
    full_text += f"● Locatie: Huis van de Gemeente Dronten, De Rede 1, Dronten\n\n"
    full_text += "1. Opening en vaststellen agenda\n2. Permanente Campagne\n3. Mededelingen\n4. Notulen\n5. Actualiteit\n"
    
    full_text += f"6. Beeldvormend\n" + ("\n".join([f"   - {i}" for i in sections["Beeldvormend"]]) if sections["Beeldvormend"] else "   - Geen items") + "\n\n"
    full_text += f"7. Oordeelsvormend\n" + ("\n".join([f"   - {i}" for i in sections["Oordeelsvormend"]]) if sections["Oordeelsvormend"] else "   - Geen items") + "\n\n"
    full_text += f"8. Raadsvergadering\n" + ("\n".join([f"   - {i}" for i in sections["Raadsvergadering"]]) if sections["Raadsvergadering"] else "   - Geen items") + "\n\n"
    full_text += "9. Rondvraag en Sluiting"

    # 3. Batch Update: Tekst + Logo (102 PT = ~3.6cm)
    requests = [
        {'insertText': {'location': {'index': 1}, 'text': full_text}},
        {
            'insertInlineImage': {
                'location': {'index': 1},
                'uri': LOGO_URL,
                'objectSize': {
                    'height': {'magnitude': 102, 'unit': 'PT'},
                    'width': {'magnitude': 102, 'unit': 'PT'}
                }
            }
        }
    ]
    docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()

    # 4. Delen
    for email in EMAILS_TO_SHARE:
        drive_service.permissions().create(fileId=doc_id, body={'type': 'user', 'role': 'writer', 'emailAddress': email}).execute()
    
    print(f"Document succesvol gemaakt: https://docs.google.com/document/d/{doc_id}/edit")

if __name__ == "__main__":
    create_google_doc()
