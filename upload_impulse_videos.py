#!/usr/bin/env python3
import os
import json
import io
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SACreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1) YouTube: load OAuth token for Impulse from secret ─────────────────────
yt_info = json.loads(os.environ['IMPULSE_YT_TOKEN'])
creds   = Credentials.from_authorized_user_info(
    yt_info,
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
youtube = build('youtube', 'v3', credentials=creds)

# ── 2) Drive: load service account from secret ──────────────────────────────
sa_info     = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
drive_creds = SACreds.from_service_account_info(
    sa_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build('drive', 'v3', credentials=drive_creds)

# ── 3) Folder ID for “Impulse” videos ────────────────────────────────────────
FOLDER_ID = '<YOUR_IMPULSE_FOLDER_ID>'  # ← replace with your actual Drive folder ID

# ── 4) List & process every file in that folder ─────────────────────────────
page_token = None
while True:
    resp = drive_service.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed=false",
        spaces='drive',
        fields='nextPageToken, files(id,name)',
        pageSize=1000,
        pageToken=page_token
    ).execute()
    files = resp.get('files', [])
    if not files:
        print("ℹ️ No videos found in Impulse folder.")
        break

    for file in files:
        file_id = file['id']
        name    = file['name']
        print(f"🔽 Downloading {name}")

        # Download to local file
        request    = drive_service.files().get_media(fileId=file_id)
        with io.FileIO(name, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    print(f"   Download {int(status.progress() * 100)}%")
        fh.close()

        # Sanitize & truncate title
        base_title = os.path.splitext(name)[0]
        if len(base_title) > 90:
            base_title = base_title[:87].rstrip() + "..."
        title = f"{base_title} #shorts"

        # Upload as YouTube Short
        print(f"📤 Uploading {name} as YouTube Short with title: {title}")
        body = {
            'snippet': {
                'title':       title,
                'description': 'Enjoy! #Impulse #Shorts',
                'tags':        ['Impulse', 'Shorts']
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

        # Delete from Drive
        print(f"🗑️ Deleting {name} from Drive")
        drive_service.files().delete(fileId=file_id).execute()

        # Remove local copy
        os.remove(name)
        print(f"✅ Completed upload and cleanup for {name}")

    page_token = resp.get('nextPageToken')
    if not page_token:
        break

print("✅ All Impulse videos processed.")
