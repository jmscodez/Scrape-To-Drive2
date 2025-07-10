import os
import re
import subprocess
import json
import praw
import yt_dlp
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sheets_client import add_video_to_sheet

# [ ... The rest of the file is unchanged until the main processing loop ... ]

# ------------------ Main ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0"
)

if __name__ == "__main__":
    drive = authenticate_drive()
    folder_id = get_or_create_folder(drive, "Impulse")
    processed = 0
    target = 3
    
    print("Starting NFL video processing...")
    print(f"Searching for {target} videos in /r/NFL, checking up to 150 posts.")

    # Increased limit from 50 to 150 for more resilience
    for i, post in enumerate(reddit.subreddit("NFL").top(time_filter="day", limit=150)):
        if processed >= target:
            print(f"Target of {target} videos reached. Exiting.")
            break
            
        print(f"\\n--- Checking post {i+1}: '{post.title}' ---")
        
        # Detailed check for video domain
        if not any(d in post.url for d in VIDEO_DOMAINS):
            print(f"-> Skipping: URL '{post.url}' is not a recognized video domain.")
            continue

        # Detailed check for download and duration
        path, dur = download_video(post.url)
        if not path:
            print(f"-> Skipping: Video download failed for URL: {post.url}")
            continue
            
        if not (10 <= dur <= 180):
            print(f"-> Skipping: Video duration ({dur}s) is outside the 10-180s range.")
            os.remove(path) # Clean up downloaded file
            continue

        print(f"  \\_ Video downloaded successfully (Duration: {dur}s). Path: {path}")

        vert = convert_to_tiktok(path)
        os.remove(path) # Clean up original file after conversion attempt
        if not vert:
            print(f"-> Skipping: Video conversion to vertical format failed.")
            continue

        print(f"  \\_ Video converted successfully. Path: {vert}")

        headline = sanitize_filename(generate_headline(post.title))
        final = f"{headline}.mp4"
        os.rename(vert, final)
        
        print(f"  \\_ Headline generated: '{headline}'")

        upload_to_drive(drive, folder_id, final)
        
        # --- Add data to Google Sheet ---
        try:
            add_video_to_sheet(
                source="NFL",
                reddit_url=post.url,
                reddit_caption=post.title,
                drive_video_name=headline
            )
        except Exception as e:
            print(f"⚠️ Failed to add data to Google Sheet: {e}")

        os.remove(final)
        processed += 1
        print(f"✅ Processed and uploaded: {headline}")
        
    print(f"\\nFinished processing. Total videos uploaded: {processed}.")
