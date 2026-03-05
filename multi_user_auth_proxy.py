import os
import json
import time
import firebase_admin
from firebase_admin import credentials, auth
from flask import Flask, request, jsonify

# Pad configuratie
FIREBASE_CRED = "/home/guido/oriscript/serviceAccountKey.json"
SESSION_DIR = "/home/guido/dronten-raad-app/sessions"

# Initialiseer Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred)

app = Flask(__name__)

# Zorg dat de sessie map bestaat
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

def verify_token(id_token):
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token['uid']
    except Exception as e:
        print(f"Token verificatie fout: {e}")
        return None

@app.route('/save_session', methods=['POST'])
def save_session():
    data = request.json
    uid = verify_token(data.get('id_token'))
    
    if not uid:
        return jsonify({"status": "error", "message": "Niet geautoriseerd"}), 401

    session_data = {
        "uid": uid,
        "cookies": data.get('cookies'),
        "updated_at": time.time()
    }

    file_path = os.path.join(SESSION_DIR, f"{uid}.json")
    with open(file_path, 'w') as f:
        json.dump(session_data, f)
    
    return jsonify({"status": "success"}), 200

@app.route('/check_session/<uid>', methods=['GET'])
def check_session(uid):
    file_path = os.path.join(SESSION_DIR, f"{uid}.json")
    
    if os.path.exists(file_path):
        return jsonify({"status": "valid"}), 200
    else:
        return jsonify({"status": "not_found"}), 404

@app.route('/')
def home():
    return "Pi API is online!", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)