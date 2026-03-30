# -*- coding: utf-8 -*-
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import re

# --- CONFIGURATIE ---
CRED_PATH = "/home/guido/oriscript/serviceAccountKey.json"
BASE_URL = "https://gemeenteraad.dronten.nl/api/v2"

if not firebase_admin._apps:
    cred = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

def haal_api_data_op(endpoint):
    """Haalt alle resultaten op via offset en limit."""
    alle_items = []
    offset = 0
    limit = 100 
    
    while True:
        url = f"{BASE_URL}/{endpoint}?limit={limit}&offset={offset}"
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            
            if response.status_code != 200:
                print(f"  [!] Endpoint {endpoint} gaf status {response.status_code} op offset {offset}")
                break
            
            data = response.json()
            items = []
            
            if 'result' in data and 'model' in data['result']:
                items = data['result']['model']
            elif 'result' in data and endpoint.split('?')[0].split('/')[-1] in data['result']:
                items = data['result'][endpoint.split('?')[0].split('/')[-1]]
            elif isinstance(data, list):
                items = data
            elif 'items' in data:
                 items = data['items']
                
            if not items:
                break
                
            alle_items.extend(items)
            
            if len(items) < limit:
                break
                
            offset += limit
            
        except Exception as e:
            print(f"  [X] Fout bij ophalen {endpoint}: {e}")
            break
            
    return alle_items

def sync_integraal():
    print("1. Ophalen basisdata (Rollen en Personen)...")
    roles_data = haal_api_data_op("roles")
    roles_dict = {r.get('id'): r.get('name') for r in roles_data if r.get('id')}
    
    persons_data = haal_api_data_op("persons")
    persons_dict = {p.get('id'): p for p in persons_data if p.get('id')}
    print(f"  -> {len(persons_dict)} personen gevonden.")

    print("\n2. Ophalen van Groepen (Fracties/Partijen) en hun leden...")
    groups_data = haal_api_data_op("groups")
    print(f"  -> {len(groups_data)} groepen gevonden.")
    
    # We maken een woordenboek om bij te houden welke persoon in welke fractie zit
    person_to_group = {}
    
    for group in groups_data:
        group_id = group.get('id')
        group_name = group.get('name', '')
        
        if not group_id:
            continue
            
        # Maak direct de veilige fractie ID aan
        veilig_fractie_id = re.sub(r'[^a-zA-Z0-9]', '_', group_name).lower()
        
        # Haal personen op voor deze specifieke groep via de door jou voorgestelde route
        groep_personen = haal_api_data_op(f"groups/{group_id}/persons")
        
        for gp in groep_personen:
            p_id = gp.get('id') or gp.get('personId')
            if p_id:
                person_to_group[p_id] = {
                    'groep_naam': group_name,
                    'fractieId': veilig_fractie_id
                }
                
    print(f"  -> {len(person_to_group)} persoonskoppelingen aan fracties gemaakt.")

    print("\n3. Ophalen van Posities om actieve rollen te bepalen...")
    positions_data = haal_api_data_op("positions")
    vandaag = datetime.now().isoformat()
    
    actieve_personen_opmaak = {}

    for pos in positions_data:
        person_id = pos.get('personId') or pos.get('person_id')
        role_id = pos.get('roleId') or pos.get('role_id')
        end_date = pos.get('endDate') or pos.get('end_date')
        
        if not person_id or person_id not in persons_dict:
            continue
            
        if end_date and end_date < vandaag:
            continue 
            
        persoon = persons_dict[person_id]
        role_name = roles_dict.get(role_id, 'Onbekende Rol').lower()
        
        # Haal de fractiegegevens op die we in stap 2 hebben verzameld
        fractie_info = person_to_group.get(person_id, {})
        groep_naam = fractie_info.get('groep_naam', '')
        fractie_id = fractie_info.get('fractieId', '')

        email = persoon.get('email') or persoon.get('emailAddress') or ''

        if person_id not in actieve_personen_opmaak:
            actieve_personen_opmaak[person_id] = {
                'id': str(person_id),
                'name': f"{persoon.get('firstName', '')} {persoon.get('lastName', '')}".strip(),
                'email': email.strip().lower(),
                'roles': [role_name],
                'groups': [groep_naam] if groep_naam else [],
                'fractieId': fractie_id,
                'laatst_gezien': firestore.SERVER_TIMESTAMP
            }
        else:
            if role_name not in actieve_personen_opmaak[person_id]['roles']:
                actieve_personen_opmaak[person_id]['roles'].append(role_name)

    print(f"\n4. Wegschrijven naar Firestore... ({len(actieve_personen_opmaak)} actieve personen gevonden)")
    
    # Ophalen bestaande inlog-accounts via email
    users_ref = db.collection('users')
    bestaande_users = users_ref.get()
    users_by_email = {}
    for user_doc in bestaande_users:
        user_data = user_doc.to_dict()
        if user_data and user_data.get('email'):
            users_by_email[user_data['email'].lower()] = user_doc.id

    batch = db.batch()
    aantal_persons = 0
    aantal_users_geupdate = 0
    
    for p_id, p_data in actieve_personen_opmaak.items():
        person_doc_ref = db.collection('persons').document(str(p_id))
        
        p_data['primary_role'] = p_data['roles'][0] if p_data['roles'] else ''
        p_data['primary_group'] = p_data['groups'][0] if p_data['groups'] else ''
        
        batch.set(person_doc_ref, p_data, merge=True)
        aantal_persons += 1
        
        persoon_email = p_data.get('email', '')
        if persoon_email and persoon_email in users_by_email:
            user_doc_id = users_by_email[persoon_email]
            user_doc_ref = db.collection('users').document(user_doc_id)
            
            user_update_data = {
                'roles': p_data['roles'],
                'fractieId': p_data['fractieId']
            }
            batch.set(user_doc_ref, user_update_data, merge=True)
            aantal_users_geupdate += 1

        if aantal_persons % 400 == 0:
            batch.commit()
            batch = db.batch()
            
    if aantal_persons % 400 != 0:
        batch.commit()
        
    print(f"? Succesvol {aantal_persons} personen gesynchroniseerd in de ORI 'persons' catalogus.")
    print(f"? Succesvol {aantal_users_geupdate} inlog-accounts voorzien van de juiste rechten in de 'users' tabel.")

if __name__ == "__main__":
    sync_integraal()