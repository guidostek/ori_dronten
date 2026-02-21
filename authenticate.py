from google_auth_oauthlib.flow import InstalledAppFlow
import os.path
import pickle

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/documents']

def main():
    flow = InstalledAppFlow.from_client_secrets_file('/home/guido/oriscript/client_secret.json', SCOPES)
    # Gebruik console-gebaseerde flow omdat je op een Pi werkt
    creds = flow.run_local_server(port=0)
    with open('/home/guido/oriscript/token.json', 'wb') as token:
        pickle.dump(creds, token)
    print("Authenticatie voltooid! token.json is aangemaakt.")

if __name__ == '__main__':
    main()