# -*- coding: utf-8 -*-
import logging
import re
import os
from flask import Flask, Response, request
from icalendar import Calendar, Event
from firebase_admin import credentials, firestore, initialize_app, get_app
from dateutil import parser
from datetime import timedelta, datetime
import pytz

# --- INITIALISATIE ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Het volledige pad naar de key op de Pi
CREDENTIALS_PATH = '/home/guido/oriscript/serviceAccountKey.json'

try:
    firebase_app = get_app()
except ValueError:
    if not os.path.exists(CREDENTIALS_PATH):
        logging.error(f"FOUT: {CREDENTIALS_PATH} niet gevonden!")
        exit(1)
    cred = credentials.Certificate(CREDENTIALS_PATH)
    firebase_app = initialize_app(cred)

db = firestore.client()
AMSTERDAM_TZ = pytz.timezone('Europe/Amsterdam')

def strip_html(text):
    """Verwijdert HTML tags voor een schone agenda."""
    if not text: return ""
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text)

# --- DE ALGEMENE ROUTE ---
# Deze 'catch-all' zorgt ervoor dat ELKE link werkt, met of zonder code.
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def general_feed(path):
    logging.info(f"Verzoek ontvangen voor pad: /{path}")
    
    cal = Calendar()
    cal.add('prodid', '-//Dronten Raad App//guidostek//NL')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Dronten Raadsagenda')
    
    nu = datetime.now(AMSTERDAM_TZ)

    try:
        # Haal alle vergaderingen op
        vergaderingen = db.collection('vergaderingen').stream()
        aantal_events = 0

        for meeting in vergaderingen:
            m_data = meeting.to_dict()
            m_id = str(meeting.id)
            
            # Datum en tijd bepalen
            m_date_str = m_data.get('date', '')
            m_time_str = m_data.get('startTime') or m_data.get('time', '00:00')
            
            if not m_date_str: continue

            try:
                # Flexibele parsing
                full_dt_str = f"{m_date_str} {m_time_str}".strip()
                dt_start = parser.parse(full_dt_str, dayfirst=True)
                
                if dt_start.tzinfo is None:
                    dt_start = AMSTERDAM_TZ.localize(dt_start)
                
                # Filter: Alleen vanaf nu
                if dt_start < nu: continue
                
                # Event opbouwen
                event = Event()
                event.add('summary', m_data.get('name') or m_data.get('title') or "Vergadering")
                event.add('dtstart', dt_start)
                event.add('dtend', dt_start + timedelta(hours=2))
                event.add('uid', f"meeting-{m_id}@noreply.guidostek.nl")
                
                # Locatie: Huis van de Gemeente Dronten
                full_address = "Huis van de Gemeente Dronten, De Rede 1, 8232 EE Dronten"
                loc = m_data.get('location', '')
                event.add('location', f"{loc}, {full_address}" if loc and loc.lower() != "n.v.t." else full_address)
                
                # Beschrijving (schoonmaken)
                event.add('description', strip_html(m_data.get('description', '')))
                
                cal.add_component(event)
                aantal_events += 1

            except Exception as e:
                logging.warning(f"Sla item {m_id} over wegens parse-fout: {e}")

        logging.info(f"Kalender verzonden met {aantal_events} items.")
        return Response(
            cal.to_ical(), 
            mimetype='text/calendar', 
            headers={"Content-Disposition": "attachment; filename=dronten_raad.ics"}
        )

    except Exception as e:
        logging.error(f"Fout in de feed: {e}")
        return "Fout bij genereren kalender", 500

if __name__ == '__main__':
    # We gebruiken poort 5005 om conflicten met andere software (zoals lighttpd) te vermijden
    app.run(host='0.0.0.0', port=5005, debug=False)