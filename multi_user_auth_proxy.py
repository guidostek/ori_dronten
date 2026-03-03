# -*- coding: utf-8 -*-
import os
import json
import time
import firebase_admin
from firebase_admin import credentials, auth
from flask import Flask, request, jsonify

FIREBASE_CRED = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "sessions"

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred)

app = Flask(__name__)

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

def verify_firebase_token(id_token):
    try:
        # Dit controleert of de gebruiker echt is wie hij zegt dat hij is via Firebase
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token['uid']
    except Exception:
        return None

@app.route('/proxy/save_session', methods=['POST'])
def save_session():
    """Ontvangt de cookies van de Flutter app na een geslaagde Microsoft login."""
    data = request.json
    id_token = data.get('id_token')
    cookies = data.get('cookies') # De cookies die de Flutter app heeft opgevangen

    uid = verify_firebase_token(id_token)
    if not uid:
        return jsonify({"status": "error", "message": "Niet geautoriseerd"}), 401

    if not cookies:
        return jsonify({"status": "error", "message": "Geen sessie data ontvangen"}), 400

    session_data = {
        "cookies": cookies,
        "created_at": time.time(),
        "uid": uid
    }
    
    with open(os.path.join(SESSION_DIR, f"{uid}.json"), 'w') as f:
        json.dump(session_data, f)
    
    return jsonify({"status": "success", "message": "Sessie veilig opgeslagen op de Pi"}), 200

if __name__ == "__main__":
    # We draaien intern op 5000, Nginx handelt de buitenwereld (HTTPS) af
    app.run(host='127.0.0.1', port=5000)
