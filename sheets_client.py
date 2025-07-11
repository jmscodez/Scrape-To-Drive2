import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

# --- Default Google Sheets Configuration ---
DEFAULT_SHEET_ID = '1NrMfQsP4IOkpoGiulGmFdu_lC9fhgiJuB3a0oKrbqJE'
DEFAULT_TAB_NAME = 'Sheet1'

def get_google_sheets_client():
    """
    Authenticates with Google Sheets API using service account credentials.
    """
    try:
        creds_json_str = os.environ['GOOGLE_SHEETS_CREDENTIALS']
        creds_json = json.loads(creds_json_str)
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"Error authenticating with Google Sheets: {e}")
        return None

def add_video_to_sheet(source, reddit_url, reddit_caption, drive_video_name, sheet_id=None, tab_name=None):
    """
    Adds a new row to the specified Google Sheet with video details.
    If sheet_id and tab_name are not provided, uses the default values.
    """
    client = get_google_sheets_client()
    if client:
        try:
            # Use provided sheet/tab names, or fall back to defaults
            target_sheet_id = sheet_id if sheet_id else DEFAULT_SHEET_ID
            target_tab_name = tab_name if tab_name else DEFAULT_TAB_NAME
            
            sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
            
            row_data = [source, reddit_url, reddit_caption, drive_video_name]
            
            sheet.append_row(row_data)
            print(f"Successfully added video data to sheet '{target_tab_name}' in document {target_sheet_id}.")
        except Exception as e:
            print(f"Error adding data to Google Sheet: {e}")

if __name__ == '__main__':
    # Example usage for testing
    print("Testing Google Sheets integration with default sheet...")
    add_video_to_sheet(
        "TEST",
        "http://reddit.com/test_url",
        "This is a test caption.",
        "Test Video Name.mp4"
    )
