import os
import subprocess
import requests
import json
from yt_dlp import YoutubeDL
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sheets_client import add_video_to_sheet

# ── Configuration ──────────────────────────────────────────────────────────────
GDRIVE_SERVICE_ACCOUNT = json.loads(os.environ.get("GDRIVE_SERVICE_ACCOUNT", "{}"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# Updated to use the specific folder and sheet for movie clips
DRIVE_FOLDER_ID    = "1Hxw_9MI4qHGP8EHgiQ0nLkku_NNrY4fm"
MOVIE_SHEET_ID     = "1anDed4DQBwq-rY6JLltnn1nnFUCF1Bmrli8xaksjnj0"
MOVIE_SHEET_TAB    = "Sheet1"

MODEL_ID           = "google/gemini-2.0-flash-lite-001"
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

TMP_DIR = "temp_clips"
os.makedirs(TMP_DIR, exist_ok=True)

# [ ... The rest of the file is unchanged until the main orchestration ... ]

# ── Main orchestration ─────────────────────────────────────────────────────────
def main():
    print("--- Starting Movie Clip Generation ---")
    scenes = get_target_scenes()
    if not scenes:
        print("Could not fetch scene ideas. Exiting.")
        return

    for movie, scene in scenes:
        final_filename = f"{movie} - {scene}.mp4".replace("/", "_")
        print(f"\\n→ Processing: {final_filename}")
        
        if already_uploaded(final_filename):
            print("  \\_ Already uploaded, skipping.")
            continue
            
        download_path = download_clip(f"{movie} {scene} scene")
        if not download_path:
            continue
            
        output_path = os.path.join(TMP_DIR, final_filename)
        
        transformed_path = transform_clip(download_path, output_path, movie)
        
        if transformed_path:
            upload_to_drive(transformed_path, final_filename)
            # Log to the dedicated Movie Clips Google Sheet
            try:
                add_video_to_sheet(
                    source="Movie",
                    reddit_url=f"youtube.com (search: {movie} {scene})",
                    reddit_caption=f"{movie} - {scene}",
                    drive_video_name=final_filename.replace('.mp4',''),
                    sheet_id=MOVIE_SHEET_ID,
                    tab_name=MOVIE_SHEET_TAB
                )
                print("  \\_ Logged to Google Sheet.")
            except Exception as e:
                print(f"❌ Failed to log to Google Sheet: {e}")
        
        # Cleanup
        if os.path.exists(download_path):
            os.remove(download_path)
        if os.path.exists(output_path):
            os.remove(output_path)
    
    print("\\n--- Movie Clip Generation Complete ---")


if __name__ == "__main__":
    main()
