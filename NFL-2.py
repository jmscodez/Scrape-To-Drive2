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
from sheets_client import add_video_to_sheet # <-- Import the new function

# ------------------ Google Drive Integration ------------------
SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])

def authenticate_drive():
    creds = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)

def get_or_create_folder(drive_service, folder_name):
    q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive_service.files().list(q=q, fields="files(id)").execute()
    items = res.get('files', [])
    if items:
        return items[0]['id']
    folder = drive_service.files().create(
        body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'},
        fields='id'
    ).execute()
    return folder['id']

def upload_to_drive(drive_service, folder_id, file_path):
    name = os.path.basename(file_path)
    media = MediaFileUpload(file_path)
    drive_service.files().create(
        body={'name': name, 'parents': [folder_id]},
        media_body=media
    ).execute()
    print(f"Uploaded {name} to Google Drive")


# ------------------ Utils ------------------
def sanitize_filename(fn):
    fn = re.sub(r'[\\\\/*?:\\"<>|]', "", fn)
    return fn.strip()[:100]

def get_video_resolution(path):
    cmd = [
        'ffprobe','-v','error',
        '-select_streams','v:0',
        '-show_entries','stream=width,height',
        '-of','csv=s=x:p=0',
        path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and 'x' in proc.stdout:
        w, h = proc.stdout.strip().split('x')
        return int(w), int(h)
    return None, None


# ------------------ Download & Convert ------------------
VIDEO_DOMAINS = {
    'reddit.com','v.redd.it','youtube.com','youtu.be',
    'streamable.com','gfycat.com','imgur.com','tiktok.com',
    'instagram.com','twitter.com','x.com','twitch.tv',
    'dailymotion.com','rumble.com'
}

def download_video(url):
    ydl_opts = {
        'outtmpl': '%(id)s.%(ext)s',
        'format': 'bestvideo[height<=1080]+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'cookiefile': 'cookies.txt',
        'force_ipv4': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'https://www.reddit.com/'
        },
        'extractor_args': {'reddit': {'skip_auth': True}}
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            # verify audio
            res = subprocess.run(
                ['ffprobe','-v','error','-select_streams','a',
                 '-show_entries','stream=codec_type','-of','csv=p=0', fn],
                stdout=subprocess.PIPE, text=True
            )
            if 'audio' not in res.stdout:
                os.remove(fn)
                return None, 0
            return fn, info.get('duration', 0)
    except Exception as e:
        print(f"❌ Download failed for {url}: {e}")
        return None, 0

def convert_to_tiktok(video_path):
    w, h = get_video_resolution(video_path)
    if not w or not h or abs(w/h - 9/16) < 0.02:
        cmd = [
            'ffmpeg','-i',video_path,
            '-vf','scale=1080:1920:force_original_aspect_ratio=increase,'
                 'crop=1080:1920,setsar=1',
            '-c:v','libx264','-preset','fast','-crf','23',
            '-c:a','aac','-y', video_path.replace(".mp4","_VERTICAL.mp4")
        ]
    else:
        sq = min(w, h)
        x_off = (w - sq) / 2
        y_off = (h - sq) / 2
        filt = (
            f"split=2[bgsrc][fgsrc];"
            f"[bgsrc]crop={sq}:{sq}:{x_off}:{y_off},"
            f"scale=1080:1920,setsar=1,gblur=sigma=20[bg];"
            f"[fgsrc]crop={sq}:{sq}:{x_off}:{y_off},"
            f"scale=1080:1080,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1"
        )
        cmd = [
            'ffmpeg','-i',video_path,
            '-vf', filt,
            '-c:v','libx264','-preset','fast','-crf','23',
            '-c:a','aac','-y', video_path.replace(".mp4","_VERTICAL.mp4")
        ]
    try:
        subprocess.run(cmd, check=True)
        return video_path.replace(".mp4","_VERTICAL.mp4")
    except Exception as e:
        print(f"❌ Conversion failed: {e}")
        return None


# ------------------ Headline Generation ------------------
def generate_headline(post_title):
    try:
        truncated_title = post_title[:200]
        prompt = (
            "Your job is to take captions that I give you and turn them into a headline that would be used on a TikTok video. "
            "I will give you the input at the end, your output should ONLY be the new title. "
            "There should be nothing else besides the caption as your output. Here are some rules to follow:\\n"
            "1. It should be the text at the top or bottom of the video that explains what's happening in the video.\\n"
            "2. It should be tailored for TikTok SEO.\\n"
            "3. Remove any '_VERTICAL.mp4' text if present.\\n"
            "4. Use a max of 2 emojis.\\n"
            "5. NO HASHTAGS.\\n"
            "6. If a name is included, keep the name in the caption.\\n\\n"
            "Here is an example input and output:\\n\\n"
            "Input: '[Highlight] Player X makes an incredible catch_VERTICAL'\\n"
            "Output: 'Player X Makes an Epic Catch 🏈🔥'\\n\\n"
            f"Now create a TikTok caption for this content: '{truncated_title}'"
        )
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemini-2.0-flash-lite-001",
            "messages": [
                {"role":"system","content":"You are a social media expert who creates viral TikTok captions for NFL content."},
                {"role":"user","content":prompt}
            ],
            "max_tokens":100,
            "temperature":0.7
        }
        res = requests.post("https://openrouter.ai/api/v1/chat/completions",
                            json=payload, headers=headers)
        res.raise_for_status()
        caption = res.json()['choices'][0]['message']['content'].strip()
        caption = re.sub(r'_VERTICAL\\.mp4','', caption)
        caption = re.sub(r'#\\w+','', caption)
        return caption[:150]
    except Exception as e:
        print(f"⚠️ Headline failed: {e}")
        return sanitize_filename(post_title)[:100]


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
            if os.path.exists(path):  # Clean up downloaded file
                os.remove(path)
            continue

        print(f"  \\_ Video downloaded successfully (Duration: {dur}s). Path: {path}")

        vert = convert_to_tiktok(path)
        os.remove(path)  # Clean up original file after conversion attempt
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