# -*- coding: utf-8 -*-
import firebase_admin
from firebase_admin import credentials, firestore
from config import CRED_PATH
import logging

def get_db():
    """Initialiseert Firebase en retourneert de Firestore client."""
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        logging.error(f"Fout bij het initialiseren van Firebase: {e}")
        raise

# Maak een globale db-instantie beschikbaar voor imports
db = get_db()