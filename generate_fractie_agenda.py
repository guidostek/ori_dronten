# -*- coding: utf-8 -*-
import os
import pickle
import string
import re
import requests
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow

# --- CONFIGURATIE ---
TOKEN_PATH = '/home/guido/oriscript/token.json'
CLIENT_SECRET_FILE = '/home/guido/oriscript/client_secret.json'
FIREBASE_CRED = "/home/guido/oriscript/serviceAccountKey.json"
PARENT_FOLDER_ID = "1MARBTjrewGulSNgCqzAE3GWd22lFfKMn"
LOGO_URL = "https://guidostek.nl/logovvd.png"
EMAILS_TO_SHARE = ["guidostek@gmail.com", "secretariaat@vvddronten.nl"]
SCOPES = ['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/drive']

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ... (Houd de functies format_dutch_date, create_case_sensitive_slug, get_meeting_full_url hetzelfde) ...

def format_dutch_date(date_obj):
    months = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]
    if isinstance(date_obj, str):
        try: date_obj = datetime.fromisoformat(date_obj.replace('Z', '+00:00'))
        except: return date_obj
    return f"{date_obj.day} {months[date_obj.month - 1]} {date_obj.year}"

def create_case_sensitive_slug(text):
    text = re.sub(r'[^a-zA-Z0-9\s-]', '', text)
    return re.sub(r'[\s-]+', '-', text).strip('-')

def get_meeting_full_url(meeting_id):
    try:
        resp = requests.get(f"https://gemeenteraad.dronten.nl/api/v1/meetings/{meeting_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json().get('fullUrl', '')
    except: pass
    return ""

def get_fractie_vergaderdatum(meeting_info):
    now = datetime.now()
    target_date = None
    for m in meeting_info:
        if m['raw_date'].date() >= now.date():
            if "Oordeelsvormend" in m['label'] or "Raad" in m['label']:
                target_date = m['raw_date']
                break
    if not target_date and meeting_info:
        for m in reversed(meeting_info):
            if "Oordeelsvormend" in m['label'] or "Raad" in m['label']:
                target_date = m['raw_date']
                break
    if not target_date: return None
    monday_of_week = target_date - timedelta(days=target_date.weekday())
    return monday_of_week + timedelta(days=1)

def get_aggregated_meeting_data():
    docs = db.collection('vergaderingen') \
             .where(filter=firestore.FieldFilter('synced', '==', True)) \
             .order_by('date', direction=firestore.Query.ASCENDING) \
             .limit(15).get()
    unique_items = {}
    meeting_info = []
    exclude_keywords = ["opening", "sluiting", "vaststellen agenda", "vaststellen verslagen", "opening en mededelingen", "vaststellen kort verslag", "afdoening ingekomen stukken", "vragen van raadsleden", "schorsing", "lta", "c-brieven", "follow-up"]
    for doc in docs:
        data = doc.to_dict()
        m_id = data.get('id')
        m_full_type = data.get('type', '')
        m_date = data.get('date')
        if not isinstance(m_date, datetime):
            m_date = datetime.fromisoformat(str(m_date).replace('Z', '+00:00'))
        base_url = get_meeting_full_url(m_id)
        m_short = "Raad" if "Raad" in m_full_type else ("Oordeelsvormend" if "Oordeel" in m_full_type else "Beeldvormend")
        label = f"{m_short} ({format_dutch_date(m_date)})"
        meeting_info.append({'label': label, 'url': base_url, 'raw_date': m_date})
        abbr = "R" if "Raad" in m_full_type else ("O" if "Oordeel" in m_full_type else "B")
        current_section_is_akkoord = False
        for item in data.get('items', []):
            title = item.get('title', '').strip()
            if any(word in title.lower() for word in exclude_keywords): continue
            if "akkoordstukken" in title.lower(): current_section_is_akkoord = True; continue
            if "bespreekstukken" in title.lower(): current_section_is_akkoord = False; continue
            item_link = f"{base_url}/{create_case_sensitive_slug(title)}" if base_url else ""
            if title not in unique_items:
                unique_items[title] = {'types': {}, 'is_akkoord': current_section_is_akkoord}
            unique_items[title]['types'][abbr] = item_link
    return meeting_info, unique_items

def create_google_doc():
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, 'rb') as token: creds = pickle.load(token)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_PATH, 'wb') as token: pickle.dump(creds, token)
            except Exception as e:
                print(f"Refresh failed: {e}. Re-authenticating...")
                creds = None
        
        if not creds:
            # Belangrijk: access_type='offline' en prompt='consent' voor een permanent refresh token
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0, access_type='offline', prompt='consent')
            with open(TOKEN_PATH, 'wb') as token: pickle.dump(creds, token)

    meeting_info, agenda_items = get_aggregated_meeting_data()
    fractie_date = get_fractie_vergaderdatum(meeting_info)

    if not fractie_date:
        print("Geen vergaderingen gevonden om een agenda voor te maken.")
        return

    # ... rest van de Google Docs logica (hetzelfde als in de vorige stap) ...
    meeting_date_str = format_dutch_date(fractie_date)
    docs_service = build('docs', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)

    file_metadata = {'name': f"Agenda fractievergadering VVD - {meeting_date_str}", 'mimeType': 'application/vnd.google-apps.document', 'parents': [PARENT_FOLDER_ID]}
    doc_file = drive_service.files().create(body=file_metadata, fields='id').execute()
    doc_id = doc_file.get('id')

    full_text = "\n"
    title_line = "Agenda fractievergadering VVD Dronten\n"
    meta_info = f"Datum: dinsdag {meeting_date_str}\nTijd: 19:30 uur\nLocatie: Huis van de Gemeente\n\n"
    body_points = "1. Opening en vaststellen agenda\n2. Permanente Campagne\n   a. Socials\n   b. Nieuwsbrief\n   c. Canvassen\n3. Mededelingen\n4. Notulen vorige vergadering\n5. Actualiteit\n6. Raad\n"
    cyclus_line = "   Betreft de cyclus:\n"

    hyperlink_targets = []
    full_text += title_line + meta_info + body_points + cyclus_line

    for m in meeting_info:
        start_idx = len(full_text) + 1
        line = f"   - {m['label']}\n"
        full_text += line
        hyperlink_targets.append((start_idx + 5, start_idx + len(line) - 1, m['url']))

    akkoord_items = {k: v for k, v in agenda_items.items() if v['is_akkoord']}
    bespreek_items = {k: v for k, v in agenda_items.items() if not v['is_akkoord']}

    full_text += "   a. Akkoordstukken (Besluiten zonder bespreking)\n"
    for idx, (title, info) in enumerate(akkoord_items.items(), 1):
        line_start_idx = len(full_text) + 1
        sorted_types = sorted(info['types'].keys())
        type_str = ",".join(sorted_types)
        full_line = f"      6.a.{idx} [{type_str}] {title}\n"
        for t in sorted_types:
            letter_pos_in_line = full_line.find(f"[{type_str}]") + 1 + type_str.find(t)
            char_abs_pos = line_start_idx + letter_pos_in_line
            hyperlink_targets.append((char_abs_pos, char_abs_pos + 1, info['types'][t]))
        full_text += full_line

    full_text += "      Bespreekstukken\n"
    alphabet = list(string.ascii_lowercase)
    for idx, (title, info) in enumerate(bespreek_items.items()):
        line_start_idx = len(full_text) + 1
        current_letter = alphabet[(idx + 1) % 26]
        sorted_types = sorted(info['types'].keys())
        type_str = ",".join(sorted_types)
        full_line = f"   {current_letter}. [{type_str}] {title}\n"
        for t in sorted_types:
            letter_pos_in_line = full_line.find(f"[{type_str}]") + 1 + type_str.find(t)
            char_abs_pos = line_start_idx + letter_pos_in_line
            hyperlink_targets.append((char_abs_pos, char_abs_pos + 1, info['types'][t]))
        full_text += full_line

    full_text += "\n7. Rondvraag en Sluiting"

    requests = [{'insertText': {'location': {'index': 1}, 'text': full_text}}]
    for start, end, url in hyperlink_targets:
        if url:
            requests.append({'updateTextStyle': {
                'range': {'startIndex': start, 'endIndex': end},
                'textStyle': {'link': {'url': url}},
                'fields': 'link'
            }})

    requests.append({'updateTextStyle': {'range': {'startIndex': 2, 'endIndex': 2 + len(title_line)}, 'textStyle': {'foregroundColor': {'color': {'rgbColor': {'red': 1.0, 'green': 0.39, 'blue': 0.0}}}, 'bold': True, 'fontSize': {'magnitude': 14, 'unit': 'PT'}}, 'fields': 'foregroundColor,bold,fontSize'}})
    requests.extend([{'insertInlineImage': {'location': {'index': 1}, 'uri': LOGO_URL, 'objectSize': {'height': {'magnitude': 75, 'unit': 'PT'}, 'width': {'magnitude': 75, 'unit': 'PT'}}}}, {'updateParagraphStyle': {'range': {'startIndex': 1, 'endIndex': 2}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}}])

    docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
    for email in EMAILS_TO_SHARE:
        drive_service.permissions().create(fileId=doc_id, body={'type': 'user', 'role': 'writer', 'emailAddress': email}).execute()

    print(f"Gereed! Agenda voor {meeting_date_str} aangemaakt.")

if __name__ == "__main__":
    create_google_doc()