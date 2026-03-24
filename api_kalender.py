# -*- coding: utf-8 -*-
import os
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta
from flask import Flask, Response, abort
from icalendar import Calendar, Event
import pytz
import logging

# Maak de log-map aan als deze nog niet bestaat
os.makedirs("/home/guido/logs", exist_ok=True)

# --- GLOBALE LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/guido/logs/api_kalender.log"),
        logging.StreamHandler()
    ]
)

logging.info("--- Start API Kalender Service ---")

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"

# --- FIREBASE INITIALISATIE ---
db = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(CRED_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Verbinden met Firebase geslaagd voor kalender API.")
except Exception as e:
    logging.error(f"FATALE FOUT bij Firebase initialisatie: {e}")

# --- FLASK APP INITIALISATIE ---
app = Flask(__name__)

@app.route('/calendar/<user_token>.ics', methods=['GET'])
def get_calendar_feed(user_token):
    logging.info(f"Kalender feed opgevraagd voor token: {user_token}")
    
    if db is None:
        abort(500, description="Database verbinding momenteel niet beschikbaar.")

    # 1. Beveiliging: Verifieer token in 'users' collectie
    try:
        users_ref = db.collection('users').where('calendarToken', '==', user_token).limit(1).stream()
        user_doc = next(users_ref, None)
        
        if not user_doc:
            logging.warning(f"Ongeldige kalender aanvraag. Token niet gevonden: {user_token}")
            abort(401, description="Ongeldig of ontbrekend token. Toegang geweigerd.")
    except Exception as e:
        logging.error(f"Database fout tijdens token validatie: {e}")
        abort(500, description="Interne server fout.")
    
    # 2. Kalender opbouwen
    cal = Calendar()
    cal.add('prodid', '-//Dronten Raad App//Agenda Sync//NL')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Raad Dronten')
    cal.add('calscale', 'GREGORIAN')
    
    # 3. Vergaderingen ophalen (timeout via de Firebase SDK is hier impliciet, maar we vangen het af)
    try:
        vergaderingen_ref = db.collection('vergaderingen').stream()
    except Exception as e:
        logging.error(f"Kon vergaderingen niet ophalen: {e}")
        abort(500, description="Kan data niet laden.")

    tz = pytz.timezone("Europe/Amsterdam")
    
    for meeting in vergaderingen_ref:
        data = meeting.to_dict()
        m_date_str = data.get('date')
        m_time_str = data.get('startTime', '00:00')
        
        if not m_date_str:
            continue
            
        try:
            start_dt_str = f"{m_date_str} {m_time_str}"
            if not m_time_str.strip():
                start_dt = datetime.strptime(m_date_str, "%Y-%m-%d")
            else:
                start_dt = datetime.strptime(start_dt_str.strip(), "%Y-%m-%d %H:%M")
            start_dt = tz.localize(start_dt)
        except (ValueError, TypeError):
            continue
            
        event = Event()
        event.add('summary', data.get('title', 'Vergadering'))
        event.add('dtstart', start_dt)
        event.add('dtend', start_dt + timedelta(hours=2))
        
        specifieke_locatie = data.get('location', '').strip()
        standaard_adres = "Huis van de Gemeente Dronten, De Rede 1, 8232 EE Dronten"
        volledige_locatie = f"{specifieke_locatie}, {standaard_adres}" if specifieke_locatie else standaard_adres
        event.add('location', volledige_locatie)
        
        meeting_id = meeting.id
        description = f"Bekijk details of claim woordvoerderschap in de app:\nhttps://raaddronten.guidostek.nl/meeting/{meeting_id}"
        event.add('description', description)
        event.add('uid', f"meeting-{meeting_id}@raaddronten.guidostek.nl")
        cal.add_component(event)
        
    logging.info(f"Kalender feed succesvol gegenereerd voor token: {user_token}")
    return Response(
        cal.to_ical(), 
        mimetype='text/calendar', 
        headers={"Content-Disposition": "attachment; filename=raad_dronten.ics"}
    )

if __name__ == "__main__":
    logging.info("Start de Flask webserver op poort 5000...")
    app.run(host='0.0.0.0', port=5000)