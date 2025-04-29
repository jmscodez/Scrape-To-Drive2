#!/usr/bin/env python3
import os
import json
import io
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SACreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1) Load YouTube OAuth token from secret ─────────────────────────────────
yt_info = json.loads(os.environ['VIRALPUPS_YT_TOKEN'])
creds    = Credentials.from_authorized_user_info(
    yt_info,
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
youtube = build('youtube', 'v3', credentials=creds)

# ── 2) Load Drive service‐account key from secret ───────────────────────────
sa_info      = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
drive_creds  = SACreds.from_service_account_info(
    sa_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build('drive', 'v3', credentials=drive_creds)

# ── 3) Your “Dog Videos” Drive folder ID ─────────────────────────────────────
FOLDER_ID = '12xiVWGcrWXnMGha2L4EegCR5jPUxhYr6'

# ── 4) List up to 1 file in that folder ──────────────────────────────────────
response = drive_service.files().list(
    q=f"'{FOLDER_ID}' in parents and trashed=false",
    spaces='drive',
    fields='files(id,name)',
    pageSize=1
).execute()
files = response.get('files', [])

if not files:
    print("ℹ️ No videos found in Drive folder.")
    exit(0)

file = files[0]
file_id = file['id']
name    = file['name']
print(f"🔽 Downloading {name}")

# ── 5) Download the file ─────────────────────────────────────────────────────
request  = drive_service.files().get_media(fileId=file_id)
fh       = io.FileIO(name, mode='wb')
downloader = MediaIoBaseDownload(fh, request)
done = False
while not done:
    status, done = downloader.next_chunk()
    if status:
        print(f"   Download {int(status.progress() * 100)}%")
fh.close()

# ── 6) Upload to YouTube as a Short ───────────────────────────────────────────
print(f"📤 Uploading {name} as YouTube Short")
body = {
    'snippet': {
        'title':       f"{name} #shorts",
        'description': 'Enjoy! #ViralPups #Dogs #Shorts',
        'tags':        ['ViralPups','Dogs','Shorts']
    },
    'status': {
        'privacyStatus': 'public'
    }
}
media = MediaFileUpload(name, mimetype='video/*')
youtube.videos().insert(
    part='snippet,status',
    body=body,
    media_body=media
).execute()

# ── 7) Delete from Drive ─────────────────────────────────────────────────────
print(f"🗑️ Deleting {name} from Drive")
drive_service.files().delete(fileId=file_id).execute()

# ── 8) Clean up local file ───────────────────────────────────────────────────
os.remove(name)
print(f"✅ Done with {name}")
