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
            'outtmpl': '%(id)s.%(ext)s',  # Use post ID instead of title to avoid long filenames
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/122.0.0.0 Safari/537.36'
            },
            'extractor_args': {
                'reddit': {
                    'skip_auth': True  # Skip authentication which might be causing JSON issues
                }
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
            "Your job is to take captions that I give you and turn them into a headline that would be used on a TikTok video. "
            "I will give you the input at the end, your output should ONLY be the new title. "
            "There should be nothing else besides the caption as your output. Here are some rules to follow:\n"
            "1. It should be the text at the top or bottom of the video that explains what's happening in the video.\n"
            "2. It should be tailored for TikTok SEO.\n"
            "3. Remove any '_VERTICAL.mp4' text if present.\n"
            "4. Use a max of 2 emojis.\n"
            "5. NO HASHTAGS.\n"
            "6. If a name is included, keep the name in the caption.\n\n"
            "Here is an example input and output:\n\n"
            "Input: '[Highlight] Cam Ward throws a dime at Miami Pro Day_VERTICAL'\n"
            "Output: 'Cam Ward Drops a DIME at Miami Pro Day 🏈🔥'\n\n"
            f"Now create a TikTok caption for this content: '{truncated_title}'"
        )
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [{
                "role": "system", 
                "content": "You are a social media expert who creates viral TikTok captions for NBA content."
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
        caption = re.sub(r'_VERTICAL\.mp4', '', caption)  # Remove any remaining _VERTICAL.mp4
        caption = re.sub(r'#\w+', '', caption)  # Remove any hashtags
        return caption[:150]  # Ensure we don't return overly long captions
        
    except Exception as e:
        print(f"⚠️ Headline generation failed: {str(e)}")
        return sanitize_filename(post_title)[:100]  # Fallback to sanitized title

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
    folder_id = get_or_create_folder(drive_service, "Impulse")

    for post in reddit.subreddit("NBA").top(time_filter="day", limit=50):
        if processed >= target:
            break
            
        try:
            # Skip if not a video domain
            if not any(domain in post.url for domain in VIDEO_DOMAINS):
                continue
                
            video_path, duration = download_video(post.url)
            if not video_path or not (10 <= duration <= 180):
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
                print(f"✅ Processed: {sanitized_headline}")

        except Exception as e:
            print(f"⚠️ Error processing post {post.id}: {str(e)}")

    print(f"\n🎉 Completed: {processed}/{target} videos processed")
