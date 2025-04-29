#!/usr/bin/env python3
import os
import json
import io
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1) YouTube: load OAuth token from secret ────────────────────────────────
yt_info = json.loads(os.environ['VIRALPUPS_YT_TOKEN'])
creds   = Credentials.from_authorized_user_info(
    yt_info,
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
youtube = build('youtube', 'v3', credentials=creds)

# ── 2) Drive: load service account from secret ──────────────────────────────
sa_info     = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
drive_creds = ServiceAccountCredentials.from_service_account_info(
    sa_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build('drive', 'v3', credentials=drive_creds)

# ── 3) Folder ID for “Dog Videos” ───────────────────────────────────────────
FOLDER_ID = '12xiVWGcrWXnMGha2L4EegCR5jPUxhYr6'

# ── 4) List up to one file in that folder ──────────────────────────────────
resp = drive_service.files().list(
    q=f"'{FOLDER_ID}' in parents and trashed=false",
    fields="files(id,name)",
    pageSize=1
).execute()
files = resp.get('files', [])

if not files:
    print("ℹ️ No videos found to upload.")
    exit(0)

file_id = files[0]['id']
name    = files[0]['name']
print(f"🔽 Downloading {name}")

# ── 5) Download it locally ──────────────────────────────────────────────────
request    = drive_service.files().get_media(fileId=file_id)
with io.FileIO(name, 'wb') as fh:
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"   Download {int(status.progress() * 100)}%")

# ── 6) Upload as YouTube Short ──────────────────────────────────────────────
print(f"📤 Uploading {name} to YouTube Shorts")
body = {
    'snippet': {
        'title':       f"{name} #shorts",
        'description': 'Enjoy! #ViralPups #Dogs #Shorts',
        'tags':        ['ViralPups', 'Dogs', 'Shorts']
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

# ── 7) Delete from Drive ────────────────────────────────────────────────────
print(f"🗑️ Deleting {name} from Drive")
drive_service.files().delete(fileId=file_id).execute()

# ── 8) Remove local file ────────────────────────────────────────────────────
os.remove(name)
print(f"✅ Completed upload and cleanup for {name}")
