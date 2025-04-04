import os
import re
import time
import subprocess
import json
import praw
import yt_dlp
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------------ Google Drive Integration ------------------
SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])

def authenticate_drive():
    credentials = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=credentials)

def get_or_create_folder(drive_service, folder_name):
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    items = results.get('files', [])
    
    if items:
        return items[0]['id']
    else:
        folder = drive_service.files().create(
            body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'},
            fields='id'
        ).execute()
        return folder['id']

def upload_to_drive(drive_service, folder_id, file_path):
    try:
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return False

        file_name = os.path.basename(file_path)
        media = MediaFileUpload(file_path)
        
        # EXACT UPLOAD METHOD FROM WORKING NBA.PY
        file = drive_service.files().create(
            body={
                'name': file_name,
                'parents': [folder_id]
            },
            media_body=media,
            fields='id'
        ).execute()
        
        print(f"✅ GENUINE Upload confirmed: {file_name} (ID: {file.get('id')})")
        return True
        
    except Exception as e:
        print(f"❌ REAL Upload failed: {str(e)}")
        return False

# ------------------ Video Processing ------------------
VIDEO_DOMAINS = {
    'reddit.com', 'v.redd.it', 'youtube.com', 'youtu.be',
    'streamable.com', 'gfycat.com', 'imgur.com', 'tiktok.com',
    'instagram.com', 'twitter.com', 'x.com', 'twitch.tv',
    'dailymotion.com', 'rumble.com'
}

def sanitize_filename(filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    return filename[:100].strip()

def download_video(url):
    try:
        ydl_opts = {
            'outtmpl': '%(id)s.%(ext)s',
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'cookiefile': 'cookies.txt',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Referer': 'https://www.reddit.com/'
            },
            'extractor_args': {
                'reddit': {'skip_auth': True}
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            
            result = subprocess.run(
                ['ffprobe', '-loglevel', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', downloaded_file],
                stdout=subprocess.PIPE,
                text=True
            )
            if 'audio' not in result.stdout:
                print("⚠️ Skipping: No audio track found")
                os.remove(downloaded_file)
                return None, 0
            return downloaded_file, info.get('duration', 0)
            
    except Exception as e:
        print(f"❌ Download failed: {str(e)}")
        return None, 0

def convert_to_tiktok(video_path):
    try:
        output_path = video_path.replace(".mp4", "_VERTICAL.mp4")
        subprocess.run([
            'ffmpeg', '-i', video_path,
            '-vf', 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-y', output_path
        ], check=True)
        return output_path
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        return None

# ------------------ OpenRouter API ------------------
def generate_headline(post_title):
    try:
        truncated_title = post_title[:200]
        prompt = f"Create viral NFL TikTok caption from: {truncated_title}"
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100
        }
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                               json=payload, 
                               headers=headers)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()[:100]
    except Exception as e:
        print(f"⚠️ Headline generation failed: {str(e)}")
        return sanitize_filename(post_title)[:100]

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0 (by /u/Proof_Difficulty_396)"
)

if __name__ == "__main__":
    processed = 0
    target = 3
    
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "NFL Videos")

    print("\n" + "="*40)
    print(f"🚀 Processing {target} NFL videos")
    print("="*40)

    subreddits = ['nfl', 'nflclips', 'footballhighlights']
    for subreddit in subreddits:
        if processed >= target:
            break
            
        for post in reddit.subreddit(subreddit).top(time_filter="day", limit=25):
            if processed >= target:
                break
                
            try:
                print(f"\n=== Processing: r/{subreddit} - {post.title[:50]}... ===")
                
                if not any(domain in post.url for domain in VIDEO_DOMAINS):
                    print(f"⚠️ Skipping: Unsupported URL - {post.url}")
                    continue
                    
                video_path, duration = download_video(post.url)
                if not video_path or not (10 <= duration <= 180):
                    if video_path:
                        os.remove(video_path)
                    continue
                
                vertical_path = convert_to_tiktok(video_path)
                os.remove(video_path)
                
                if vertical_path:
                    headline = generate_headline(post.title)
                    sanitized_headline = sanitize_filename(headline)
                    final_path = f"{sanitized_headline}.mp4"
                    os.rename(vertical_path, final_path)
                    
                    # VERBOSE UPLOAD CONFIRMATION
                    if upload_to_drive(drive_service, folder_id, final_path):
                        processed += 1
                        print(f"✅ GENUINE Upload Success: {sanitized_headline}")
                    else:
                        print(f"❌ REAL Upload Failed: {sanitized_headline}")
                    
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    
                    time.sleep(2)  # Rate limiting

            except Exception as e:
                print(f"⚠️ Error processing post: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} NFL videos")
    print("="*40)
