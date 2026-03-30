import time
import json
import os
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging

# --- CONFIGURATIE (Deze mag je naar wens aanpassen) ---
# De URL waar we de data van ophalen (pas aan naar het juiste ORI eindpunt)
ORI_API_URL = "https://gemeenteraad.dronten.nl/api/v2/meetings" 
# Hoe vaak controleren we op nieuwe data? (300 seconden = 5 minuten)
CHECK_INTERVAL_SECONDS = 300  
# Het lokale bestand waarin we bijhouden welke vergaderingen we al hebben gehad
SEEN_MEETINGS_FILE = "verwerkte_meetings.json" 
# Het pad naar jouw Firebase Service Account sleutel op de Raspberry Pi
FIREBASE_KEY_PATH = "/home/guido/oriscript/serviceAccountKey.json" 

def initialize_firebase():
    """Start de connectie met Firebase zodat we in de database kunnen."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
    return firestore.client()

def load_seen_meetings():
    """Haalt de lijst op van de meetings waar we al een melding voor hebben gestuurd."""
    if os.path.exists(SEEN_MEETINGS_FILE):
        with open(SEEN_MEETINGS_FILE, 'r') as file:
            return set(json.load(file))
    return set()

def save_seen_meetings(seen_meetings):
    """Bewaart de bijgewerkte lijst, zodat we na een herstart van de Pi niet dubbel mailen."""
    with open(SEEN_MEETINGS_FILE, 'w') as file:
        json.dump(list(seen_meetings), file)

def get_all_fcm_tokens(db):
    """Leest de 'users' collectie in Firestore uit en verzamelt alle tokens."""
    tokens = []
    users_ref = db.collection('users').stream()
    for doc in users_ref:
        user_data = doc.to_dict()
        token = user_data.get('fcmToken')
        if token:
            tokens.append(token)
    return tokens

def send_push_notification(tokens, meeting_id, meeting_title):
    """Maakt en verstuurt de pushmelding naar alle telefoons via Firebase Messaging."""
    if not tokens:
        print("Geen geregistreerde telefoons gevonden om meldingen naar te sturen.")
        return

    # De melding zelf maken. We geven 'meetingId' en 'documentId' mee in de extra 'data'
    # zodat de Flutter app weet welke pagina hij moet openen.
    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=f"Nieuwe Vergadering: {meeting_title}",
            body="Er is zojuist een nieuwe vergadering beschikbaar gemaakt."
        ),
        data={
            "meetingId": str(meeting_id),
            "documentId": str(meeting_id)
        },
        tokens=tokens,
    )

    try:
        response = messaging.send_each_for_multicast(message)
        print(f"Melding verstuurd! Succesvol: {response.success_count}, Mislukt: {response.failure_count}.")
    except Exception as e:
        print(f"Fout tijdens het versturen van de pushmelding: {e}")

def monitor_ori_api(db, seen_meetings):
    """Haalt de nieuwste data op en controleert of er iets nieuws is."""
    try:
        response = requests.get(ORI_API_URL)
        response.raise_for_status()
        
        # We verwachten dat de data in JSON-formaat komt. 
        data = response.json()
        meetings = data.get('data', []) if isinstance(data, dict) else data
        
        new_items_found = False
        
        for meeting in meetings:
            meeting_id = str(meeting.get('id', ''))
            meeting_title = meeting.get('name', 'Nieuwe Vergadering')
            
            # Controleer of we dit ID voor de eerste keer zien
            if meeting_id and meeting_id not in seen_meetings:
                print(f"Nieuw item gevonden! ID: {meeting_id} | Titel: {meeting_title}")
                
                # 1. Haal de actuele lijst met tokens op
                tokens = get_all_fcm_tokens(db)
                
                # 2. Verstuur de pushmelding
                send_push_notification(tokens, meeting_id, meeting_title)
                
                # 3. Voeg de vergadering toe aan onze 'gezien' lijst
                seen_meetings.add(meeting_id)
                new_items_found = True
                
        # Als er nieuwe items waren, slaan we ons bestand op
        if new_items_found:
            save_seen_meetings(seen_meetings)
            print("De lijst met verwerkte vergaderingen is bijgewerkt.")
            
    except Exception as e:
        print(f"Oeps, er ging iets mis tijdens het controleren van de API: {e}")

def main():
    print("--- Start FCM Trigger Monitor op Pi 5 ---")
    
    # 1. Alles klaarzetten
    db = initialize_firebase()
    seen_meetings = load_seen_meetings()
    
    print(f"We kennen al {len(seen_meetings)} vergaderingen uit het verleden.")
    print(f"Elke {CHECK_INTERVAL_SECONDS} seconden controleren we op nieuwe data...\n")
    
    # 2. De grote oneindige loop die steeds blijft wachten en controleren
    while True:
        monitor_ori_api(db, seen_meetings)
        time.sleep(CHECK_INTERVAL_SECONDS)

# Dit start het script daadwerkelijk op
if __name__ == "__main__":
    main()