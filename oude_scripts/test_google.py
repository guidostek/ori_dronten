from google.oauth2 import service_account
from googleapiclient.discovery import build

GOOGLE_CRED = "/home/guido/oriscript/google_agenda_key.json"
scopes = ['https://www.googleapis.com/auth/drive.metadata.readonly']

try:
    creds = service_account.Credentials.from_service_account_file(GOOGLE_CRED, scopes=scopes)
    service = build('drive', 'v3', credentials=creds)
    results = service.files().list(pageSize=1).execute()
    print("Verbinding geslaagd! Het Service Account kan bij de Drive.")
except Exception as e:
    print(f"Fout bij verbinden: {e}")
