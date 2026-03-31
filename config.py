# -*- coding: utf-8 -*-
import os

# --- PADEN & DIRECTORIES ---
BASE_DIR = "/home/guido/oriscript"
CRED_PATH = os.path.join(BASE_DIR, "serviceAccountKey.json")

LOG_DIR = "/home/guido/logs"
os.makedirs(LOG_DIR, exist_ok=True)

# --- API ENDPOINTS ---
DRONTEN_API_V2 = "https://gemeenteraad.dronten.nl/api/v2"
BASE_URL_PUBLIC = "https://gemeenteraad.dronten.nl"

# --- CACHE BESTANDEN VOOR NOTIFICATIES ---
# Hierin houden we bij welke ID's al een ping hebben gehad
NOTIFIED_MEETINGS_FILE = os.path.join(BASE_DIR, "notified_meetings.json")
NOTIFIED_DOCS_FILE = os.path.join(BASE_DIR, "notified_docs.json")

# --- SYNC INSTELLINGEN ---
TIMEOUT_SECONDS = 20
LIMIT_MEETINGS = 20
LIMIT_DOCS = 50

# --- NIEUW: DEEP SYNC INSTELLINGEN (Taak 1.1) ---
# Deep Sync parameter ingesteld op 3 maanden voor de backend refactor
DEEP_SYNC_MONTHS = 3
# Omgezet naar dagen (ongeveer 90), wat makkelijker is voor datum-berekeningen in Python
DEEP_SYNC_DAYS = DEEP_SYNC_MONTHS * 30