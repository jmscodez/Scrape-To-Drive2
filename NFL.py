import praw
import yt_dlp
import os
import subprocess
import re
import textwrap
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive Setup
SERVICE_ACCOUNT_FILE = 'service_account.json'
SCOPES = ['https://www.googleapis.com/auth/drive']
PARENT_FOLDER_ID = '1IjWmMJJKp3BMhVINrSbwkL1HIT5277WF'  # Replace with your Google Drive folder ID

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

# Reddit API Setup
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent='script:reddit-video-downloader:v1.0'
)

def generate_short_caption(text):
    """Generate optimized caption using OpenRouter API"""
    try:
        # Clean Reddit-specific formatting
        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # OpenRouter API call
        headers = {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "openai/gpt-3.5-turbo",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a social media expert who creates viral captions from Reddit titles. Follow these rules:\n"
                              "1. Keep it 8-12 words max\n"
                              "2. Use attention-grabbing phrasing\n"
                              "3. Add context if needed\n"
                              "4. Never use markdown\n"
                              "5. Remove all brackets but keep attribution if relevant"
                },
                {
                    "role": "user",
                    "content": f"Transform this Reddit title into a viral caption: {text}"
                }
            ]
        }

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload
        )

        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].strip()
        return " ".join(text.split()[:12])  # Fallback

    except Exception as e:
        print(f"⚠️ OpenRouter caption failed: {str(e)}")
        return " ".join(text.split()[:12])

# [Rest of your existing functions remain the same: download_video, convert_to_tiktok, add_caption, upload_to_drive]

if __name__ == "__main__":
    processed = 0
    target = 5
    
    print("\n" + "="*40)
    print(f"🚀 Processing {target} videos from r/NFL")
    print("="*40)

    posts = reddit.subreddit("NFL").top(time_filter="day", limit=50)
    
    for post in posts:
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            
            if not (hasattr(post, 'is_video') and post.is_video) or hasattr(post, 'crosspost_parent'):
                print("⚠️ Skipping: Not a native Reddit video")
                continue
                
            if not hasattr(post, 'media') or not post.media or 'reddit_video' not in post.media:
                print("⚠️ Skipping: Invalid video metadata")
                continue
                
            if not post.media['reddit_video'].get('has_audio', False):
                print("⚠️ Skipping: No audio track")
                continue
                
            video_path, duration = download_video(post.url)
            if not video_path:
                continue
                
            if not (10 <= duration <= 180):
                print(f"⚠️ Skipping: Duration {duration}s out of range")
                os.remove(video_path)
                continue
            
            vertical_path = convert_to_tiktok(video_path)
            os.remove(video_path)
            final_path = add_caption(vertical_path, post.title)
            os.remove(vertical_path)
            
            # Upload to Google Drive
            drive_id = upload_to_drive(final_path)
            if drive_id:
                processed += 1
                print(f"✅ Success: {os.path.basename(final_path)}")
                os.remove(final_path)
            
        except Exception as e:
            print(f"⚠️ Error processing: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} videos processed")
    print("="*40)
