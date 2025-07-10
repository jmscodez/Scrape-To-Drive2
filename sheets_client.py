import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

# --- Google Sheets Configuration ---
SHEET_ID = '1NrMfQsP4IOkpoGiulGmFdu_lC9fhgiJuB3a0oKrbqJE'
SHEET_NAME = 'Sheet1'

def get_google_sheets_client():
    """
    Authenticates with Google Sheets API using service account credentials.
    """
    try:
        # Get credentials from GitHub Secrets
        creds_json_str = os.environ['GOOGLE_SHEETS_CREDENTIALS']
        creds_json = json.loads(creds_json_str)
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"Error authenticating with Google Sheets: {e}")
        return None

def add_video_to_sheet(source, reddit_url, reddit_caption, drive_video_name):
    """
    Adds a new row to the specified Google Sheet with video details.

    Args:
        source (str): The source of the video ('NBA' or 'NFL').
        reddit_url (str): The URL of the video on Reddit.
        reddit_caption (str): The caption of the video on Reddit.
        drive_video_name (str): The name of the video file in Google Drive.
    """
    client = get_google_sheets_client()
    if client:
        try:
            sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
            
            # Prepare the row data in the correct order
            row_data = [source, reddit_url, reddit_caption, drive_video_name]
            
            sheet.append_row(row_data)
            print(f"Successfully added video data to Google Sheet: {row_data}")
        except Exception as e:
            print(f"Error adding data to Google Sheet: {e}")

if __name__ == '__main__':
    # Example usage for testing
    # To test locally, you need to set the GOOGLE_SHEETS_CREDENTIALS environment variable
    # or modify the script to load credentials from a local file.
    print("Testing Google Sheets integration...")
    add_video_to_sheet(
        "TEST",
        "http://reddit.com/test_url",
        "This is a test caption.",
        "Test Video Name.mp4"
    )
