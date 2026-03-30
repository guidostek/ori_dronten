# -*- coding: utf-8 -*-
import logging
from firebase_admin import messaging
from firebase_client import db

def get_all_fcm_tokens():
    """Haalt alle geregistreerde FCM-tokens op uit de users collectie."""
    tokens = set()
    try:
        users_ref = db.collection('users').stream()
        for doc in users_ref:
            data = doc.to_dict()
            if 'fcmToken' in data and data['fcmToken']:
                tokens.add(data['fcmToken'])
    except Exception as e:
        logging.error(f"Fout bij ophalen van FCM tokens: {e}")
    return list(tokens)

def send_push_notification(title, body):
    """Stuurt een multicast pushbericht naar alle actieve tokens."""
    tokens = get_all_fcm_tokens()
    
    if not tokens:
        logging.warning("Geen FCM tokens gevonden in de database. Melding overgeslagen.")
        return

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body
        ),
        tokens=tokens
    )
    
    try:
        response = messaging.send_multicast(message)
        logging.info(f"FCM verzonden: {response.success_count} succesvol, {response.failure_count} mislukt.")
    except Exception as e:
        logging.error(f"Fatale fout bij verzenden van pushbericht: {e}")