import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

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
    limit = 100 # We halen 100 records per keer op
    
    while True:
        # Nu gebruiken we de correcte GO API parameters: offset en limit
        url = f"{BASE_URL}/{endpoint}?limit={limit}&offset={offset}"
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            
            if response.status_code != 200:
                print(f"  [!] Endpoint {endpoint} gaf status {response.status_code} op offset {offset}")
                break
            
            data = response.json()
            items = []
            
            # De exacte GO API v2 structuur (zoals in de PDF op pag 1)
            if 'result' in data and 'model' in data['result']:
                items = data['result']['model']
            elif 'result' in data and endpoint in data['result']:
                items = data['result'][endpoint]
            elif endpoint in data:
                items = data[endpoint]
            elif isinstance(data, list):
                items = data
            elif 'items' in data:
                 items = data['items']
                
            if not items:
                break
                
            alle_items.extend(items)
            
            # Als we minder items terugkrijgen dan we vroegen, zijn we bij het einde van de lijst!
            if len(items) < limit:
                break
                
            # Verhoog de offset voor de volgende ronde
            offset += limit
            
        except Exception as e:
            print(f"  [X] Fout bij ophalen {endpoint}: {e}")
            break
            
    return alle_items
def sync_integraal():
    print("1. Ophalen basisdata (DMUs en Roles)...")
    roles_data = haal_api_data_op("roles")
    roles_dict = {r.get('id'): r.get('name') for r in roles_data if r.get('id')}
    print(f"  -> {len(roles_dict)} unieke rollen gevonden.")
    
    dmus_data = haal_api_data_op("dmus")
    dmus_dict = {d.get('id'): d.get('name') for d in dmus_data if d.get('id')}
    print(f"  -> {len(dmus_dict)} unieke DMU's (organen/fracties) gevonden.")

    print("\n2. Ophalen van alle Personen...")
    persons_data = haal_api_data_op("persons")
    persons_dict = {p.get('id'): p for p in persons_data if p.get('id')}
    print(f"  -> {len(persons_dict)} personen gevonden.")

    print("\n3. Ophalen van Posities en bepalen van actieve status...")
    positions_data = haal_api_data_op("positions")
    print(f"  -> {len(positions_data)} totaal aantal posities gevonden (historisch + actief).")
    
    vandaag = datetime.now().isoformat()
    actieve_personen_opmaak = {}

    for pos in positions_data:
        # GO API gebruikt soms camelCase (personId) en soms snake_case (person_id). We vangen beide af.
        person_id = pos.get('personId') or pos.get('person_id')
        role_id = pos.get('roleId') or pos.get('role_id')
        dmu_id = pos.get('dmuId') or pos.get('dmu_id')
        
        # We zoeken de einddatum. Soms is dit endDate, soms end_date.
        end_date = pos.get('endDate') or pos.get('end_date')
        
        if not person_id or person_id not in persons_dict:
            continue
            
        # Filter: Is de positie in het verleden geëindigd?
        if end_date and end_date < vandaag:
            continue 
            
        # We hebben een ACTIEVE positie!
        persoon = persons_dict[person_id]
        role_name = roles_dict.get(role_id, 'Onbekende Rol')
        groep_naam = dmus_dict.get(dmu_id, '') if dmu_id else ''

        # Email adres veld check
        email = persoon.get('email') or persoon.get('emailAddress') or ''

        if person_id not in actieve_personen_opmaak:
            actieve_personen_opmaak[person_id] = {
                'id': str(person_id),
                'name': f"{persoon.get('firstName', '')} {persoon.get('lastName', '')}".strip(),
                'email': email.strip().lower(),
                'roles': [role_name],
                'groups': [groep_naam] if groep_naam else [],
                'laatst_gezien': firestore.SERVER_TIMESTAMP
            }
        else:
            if role_name not in actieve_personen_opmaak[person_id]['roles']:
                actieve_personen_opmaak[person_id]['roles'].append(role_name)
            if groep_naam and groep_naam not in actieve_personen_opmaak[person_id]['groups']:
                actieve_personen_opmaak[person_id]['groups'].append(groep_naam)

    print(f"\n4. Wegschrijven naar Firestore... ({len(actieve_personen_opmaak)} actieve personen overgebleven)")
    
    if len(actieve_personen_opmaak) == 0:
        print("  [!] Let op: Er zijn nog steeds 0 personen. Controleer de variabelen in de console output.")
        return

    batch = db.batch()
    aantal = 0
    
    for p_id, p_data in actieve_personen_opmaak.items():
        doc_ref = db.collection('persons').document(str(p_id))
        
        p_data['primary_role'] = p_data['roles'][0] if p_data['roles'] else ''
        p_data['primary_group'] = p_data['groups'][0] if p_data['groups'] else ''
        
        batch.set(doc_ref, p_data, merge=True)
        aantal += 1
        
        if aantal % 400 == 0:
            batch.commit()
            batch = db.batch()
            
    if aantal % 400 != 0:
        batch.commit()
        
    print(f"✅ Succesvol {aantal} actieve personen met rollen/fracties gesynchroniseerd!")

if __name__ == "__main__":
    sync_integraal()
