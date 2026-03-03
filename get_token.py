import pickle
import os
from google_auth_oauthlib.flow import InstalledAppFlow

# De rechten die we nodig hebben (Drive en Docs)
SCOPES = ['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/drive']

def generate_token():
    # Pad naar je credentials die je van Google hebt gedownload
    client_secrets_file = 'client_secret.json'
    
    if not os.path.exists(client_secrets_file):
        print(f"Fout: {client_secrets_file} niet gevonden in deze map!")
        return

    # Start de inlog procedure
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    
    # Dit opent je standaard browser
    creds = flow.run_local_server(port=0)

    # Sla het token op
    with open('token.json', 'wb') as token:
        pickle.dump(creds, token)
        
    print("\nSucces! Het bestand 'token.json' is aangemaakt in deze map.")
    print("Kopieer dit bestand nu naar je Raspberry Pi in de map /home/guido/oriscript/")

if __name__ == '__main__':
    generate_token()
