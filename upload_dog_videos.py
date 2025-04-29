import os, json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.http import MediaFileUpload

# ── 1) Load YouTube OAuth token from secret ──────────────────────────────
yt_info = json.loads(os.environ['VIRALPUPS_YT_TOKEN'])
creds = Credentials.from_authorized_user_info(yt_info, scopes=["https://www.googleapis.com/auth/youtube.upload"])
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
youtube = build('youtube', 'v3', credentials=creds)

# ── 2) Load Drive service-account key from secret ────────────────────────
sa_info = json.loads(os.environ['DRIVE_SA_KEY'])
drive_creds = ServiceAccountCredentials.from_service_account_info(
    sa_info, scopes=["https://www.googleapis.com/auth/drive"]
)
gauth = GoogleAuth()
gauth.credentials = drive_creds
drive = GoogleDrive(gauth)

# ── 3) Your Dog Videos folder ID ─────────────────────────────────────────
FOLDER_ID = '12xiVWGcrWXnMGha2L4EegCR5jPUxhYr6'

# ── 4) Process a single video per run ────────────────────────────────────
files = drive.ListFile({'q': f"'{FOLDER_ID}' in parents and trashed=false"}).GetList()
for f in files:
    name = f['title']
    print(f"🔽 Downloading {name}")
    f.GetContentFile(name)

    print(f"📤 Uploading {name} as YouTube Short")
    body = {
        'snippet': {
            'title':       f"{name} #shorts",
            'description': 'Enjoy! #ViralPups #Dogs #Shorts',
            'tags':        ['ViralPups','Dogs','Shorts']
        },
        'status': {'privacyStatus': 'public'}
    }
    media = MediaFileUpload(name, mimetype='video/*')
    youtube.videos().insert(part='snippet,status', body=body, media_body=media).execute()

    print(f"✂️ Deleting {name} locally & from Drive")
    os.remove(name)
    f.Delete()
    break
