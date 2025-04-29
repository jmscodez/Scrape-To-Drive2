#!/usr/bin/env python3
import os
import json
import io
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SACreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError

# 1) YouTube auth via user OAuth creds (only upload scope needed)
yt_info = json.loads(os.environ['CATS_YT_TOKEN'])
creds   = Credentials.from_authorized_user_info(
    yt_info,
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
youtube = build('youtube', 'v3', credentials=creds)

# 2) Drive via service-account for all Drive operations
sa_info     = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
drive_creds = SACreds.from_service_account_info(
    sa_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build('drive', 'v3', credentials=drive_creds)

# 3) “Cats” folder ID
FOLDER_ID = '15CwudXXMNqIrkw21PWYErNtsU2asuL5A'

# 4) List & process every file
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
        print("ℹ️ No videos found in Cats folder.")
        break

    for file in files:
        file_id = file['id']
        name    = file['name']
        print(f"🔽 Downloading {name}")

        # Download via service account
        request = drive_service.files().get_media(fileId=file_id)
        with io.FileIO(name, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    print(f"   Download {int(status.progress() * 100)}%")

        # Sanitize & truncate title
        base = os.path.splitext(name)[0]
        if len(base) > 90:
            base = base[:87].rstrip() + "..."
        title = f"{base} #shorts"

        # Upload to YouTube
        print(f"📤 Uploading {name} as YouTube Short with title: {title}")
        try:
            media = MediaFileUpload(name, mimetype='video/*')
            youtube.videos().insert(
                part='snippet,status',
                body={
                    'snippet': {
                        'title':       title,
                        'description': 'Enjoy! #Cats #Shorts',
                        'tags':        ['Cats', 'Shorts']
                    },
                    'status': {'privacyStatus':'public'}
                },
                media_body=media
            ).execute()
        except HttpError as e:
            print("❌ Upload failed:", e)
            os.remove(name)
            continue

        # Delete from Drive via service account
        print(f"🗑️ Deleting {name} from Drive via service account")
        try:
            drive_service.files().delete(fileId=file_id).execute()
        except HttpError as e:
            print("⚠️ Could not delete via service account:", e)

        # Cleanup local copy
        os.remove(name)
        print(f"✅ Completed upload & cleanup for {name}")

    page_token = resp.get('nextPageToken')
    if not page_token:
        break

print("✅ All Cats videos processed.")
