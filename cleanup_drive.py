import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load service account credentials from environment variable
service_account_info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT"])
creds = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build("drive", "v3", credentials=creds)

def list_all_file_ids():
    file_ids = []
    page_token = None
    while True:
        response = drive_service.files().list(
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            pageToken=page_token
        ).execute()
        for file in response.get("files", []):
            file_ids.append(file["id"])
        page_token = response.get("nextPageToken", None)
        if not page_token:
            break
    return file_ids

def delete_files(file_ids):
    for file_id in file_ids:
        try:
            drive_service.files().delete(fileId=file_id).execute()
            print(f"Deleted file: {file_id}")
        except Exception as e:
            print(f"Failed to delete {file_id}: {e}")

def empty_trash():
    try:
        drive_service.files().emptyTrash().execute()
        print("Trash emptied.")
    except Exception as e:
        print(f"Failed to empty trash: {e}")

if __name__ == "__main__":
    print("Listing all files...")
    files = list_all_file_ids()
    print(f"Found {len(files)} files.")
    delete_files(files)
    empty_trash()