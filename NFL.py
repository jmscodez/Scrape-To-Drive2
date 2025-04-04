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
        file_size = os.path.getsize(file_path)/1024/1024  # Size in MB
        print(f"📤 Uploading {file_name} ({file_size:.2f} MB)")

        media = MediaFileUpload(file_path, resumable=True, chunksize=1024*1024)
        request = drive_service.files().create(
            body={'name': file_name, 'parents': [folder_id]},
            media_body=media,
            fields='id,size'
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"↗️ Progress: {int(status.progress() * 100)}%")
        
        print(f"✅ Successfully uploaded {file_name} (ID: {response['id']}, Size: {int(response.get('size',0))/1024/1024:.2f} MB)")
        return True
        
    except Exception as e:
        print(f"❌ Upload failed: {str(e)}")
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
                'Referer': 'https://www.reddit.com/',
                'Origin': 'https://www.reddit.com'
            },
            'extractor_args': {
                'reddit': {'skip_auth': True},
                'youtube': {'skip': ['dash', 'hls']},
                'twitter': {'include': ['native_video']}
            },
            'sleep_interval': 5
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
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
            '-y', output_path
        ], check=True)
        return output_path
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        return None

# ------------------ OpenRouter API ------------------
def generate_headline(post_title):
    try:
        truncated_title = post_title[:200]
        prompt = (
            "Create a viral NFL TikTok caption (under 100 chars) from this:\n"
            f"'{truncated_title}'\n\n"
            "Rules:\n"
            "- No hashtags\n"
            "- Max 2 emojis\n"
            "- Keep player names\n"
            "- Exciting tone\n\n"
            "Example:\n"
            "Input: 'Mahomes crazy no-look pass vs Raiders'\n"
            "Output: 'Mahomes with the NO-LOOK DIME! 🏈🔥'"
        )
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [{
                "role": "system", 
                "content": "You create viral NFL TikTok captions"
            }, {
                "role": "user", 
                "content": prompt
            }],
            "max_tokens": 100,
            "temperature": 0.7
        }
        
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                               json=payload, 
                               headers=headers,
                               timeout=30)
        response.raise_for_status()
        
        caption = response.json()['choices'][0]['message']['content'].strip()
        caption = re.sub(r'_VERTICAL\.mp4|#\w+', '', caption)
        return caption[:150]
        
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
    target = 5
    
    # Initialize services
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "NFL Videos")
    
    # Verify drive access
    about = drive_service.about().get(fields='storageQuota').execute()
    print(f"🔍 Drive Storage: {about['storageQuota'].get('usage', '?')} / {about['storageQuota'].get('limit', '?')} bytes used")

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
                print(f"\n📭 Processing: r/{subreddit} - {post.title[:50]}...")
                
                if not any(domain in post.url for domain in VIDEO_DOMAINS):
                    print(f"⚠️ Unsupported URL: {post.url}")
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
                    
                    if upload_to_drive(drive_service, folder_id, final_path):
                        processed += 1
                        print(f"🏈 Success: {sanitized_headline}")
                    else:
                        print(f"❌ Upload failed for: {sanitized_headline}")
                    
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    
                    time.sleep(5)  # Rate limit

            except Exception as e:
                print(f"⚠️ Error processing post: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} NFL videos uploaded")
    print("="*40)
