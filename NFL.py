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
    file_name = os.path.basename(file_path)
    media = MediaFileUpload(file_path)
    drive_service.files().create(
        body={'name': file_name, 'parents': [folder_id]},
        media_body=media
    ).execute()
    print(f"Uploaded {file_name} to Google Drive")

# ------------------ Video Processing ------------------
VIDEO_DOMAINS = {
    'reddit.com', 'v.redd.it', 'youtube.com', 'youtu.be',
    'streamable.com', 'gfycat.com', 'imgur.com', 'tiktok.com',
    'instagram.com', 'twitter.com', 'x.com', 'twitch.tv',
    'dailymotion.com', 'rumble.com'
}

def sanitize_filename(filename):
    """Sanitize the filename to avoid issues with long names and special characters"""
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename[:100]  # Limit filename length
    return filename.strip()

def download_video(url):
    try:
        ydl_opts = {
            'outtmpl': '%(id)s.%(ext)s',
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/122.0.0.0 Safari/537.36'
            },
            'extractor_args': {
                'reddit': {'skip_auth': True}
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            
            # Verify the file has audio
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
        print(f"❌ Download failed for {url}: {str(e)}")
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
        # Truncate the title to avoid hitting token limits
        truncated_title = post_title[:200]
        prompt = (
            "Create a viral NFL TikTok caption from this Reddit title. Rules:\n"
            "1. Keep it 8-12 words max\n"
            "2. Use attention-grabbing phrasing\n"
            "3. Remove all brackets but keep attribution if relevant\n"
            "4. Use max 2 emojis\n"
            "5. No hashtags\n\n"
            "Example transformations:\n"
            "'[NFL Films] His name is Baun, Zack Baun...' → 'Zack Baun's iconic play! Same move, same result 🏈🔥'\n"
            "'Coach says defense needs improvement' → 'Coach demands better defense for playoffs'\n\n"
            f"Title to transform: '{truncated_title}'"
        )
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [{
                "role": "system", 
                "content": "You are a social media expert who creates viral NFL content captions"
            }, {
                "role": "user", 
                "content": prompt
            }],
            "max_tokens": 100,
            "temperature": 0.7
        }
        
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        
        # Extract and clean the response
        caption = response.json()['choices'][0]['message']['content'].strip()
        caption = re.sub(r'_VERTICAL\.mp4', '', caption)
        caption = re.sub(r'#\w+', '', caption)
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
    target = 5  # Process 5 videos per run
    
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "NFL Videos")

    print("\n" + "="*40)
    print(f"🚀 Processing {target} videos from r/NFL")
    print("="*40)

    for post in reddit.subreddit("NFL").top(time_filter="day", limit=50):
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            
            # Skip if not a video domain
            if not any(domain in post.url for domain in VIDEO_DOMAINS):
                print(f"⚠️ Skipping: Unsupported URL - {post.url}")
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

            if vertical_path:
                headline = generate_headline(post.title)
                sanitized_headline = sanitize_filename(headline)
                final_path = f"{sanitized_headline}.mp4"
                os.rename(vertical_path, final_path)
                
                upload_to_drive(drive_service, folder_id, final_path)
                os.remove(final_path)
                processed += 1
                print(f"✅ Success: {sanitized_headline}")

        except Exception as e:
            print(f"⚠️ Error processing post {post.id}: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} videos processed")
    print("="*40)
