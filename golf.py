import os
import re
import subprocess
import json

import praw
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------------ Configuration ------------------
SUBREDDIT_PRIMARY = "golf"
SUBREDDIT_FALLBACK = "golffails"
TARGET_COUNT = 5
FOLDER_NAME = "golf"

# ------------------ Drive Auth ------------------
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
    files = res.get('files', [])
    if files:
        return files[0]['id']
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
    print(f"Uploaded {name}")

# ------------------ Helpers ------------------
def sanitize_filename(fn):
    fn = re.sub(r'[\\/*?:"<>|]', "", fn)
    return fn.strip()[:100]

# ------------------ Video Download ------------------
VIDEO_DOMAINS = {
    'reddit.com','v.redd.it','youtube.com','youtu.be',
    'streamable.com','gfycat.com','imgur.com','tiktok.com',
    'instagram.com','twitter.com','x.com','twitch.tv',
    'dailymotion.com','rumble.com'
}

def download_video(url):
    opts = {
        'outtmpl': '%(id)s.%(ext)s',
        'format':'bestvideo[height<=1080]+bestaudio/best',
        'merge_output_format':'mp4',
        'quiet': True,
        'cookiefile':'cookies.txt',
        'force_ipv4': True,
        'http_headers':{'User-Agent':'Mozilla/5.0'}
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
        # verify audio
        res = subprocess.run(
            ['ffprobe','-v','error','-select_streams','a',
             '-show_entries','stream=codec_type','-of','csv=p=0', fn],
            stdout=subprocess.PIPE, text=True
        ).stdout
        if 'audio' not in res:
            os.remove(fn)
            return None, 0
        return fn, info.get('duration',0)
    except Exception as e:
        print(f"❌ Download failed for {url}: {e}")
        return None, 0

# ------------------ Convert to Vertical ------------------
def convert_to_tiktok(path):
    out = path.replace(".mp4","_VERTICAL.mp4")
    subprocess.run([
        'ffmpeg','-y','-i',path,
        '-vf','scale=1080:1920:force_original_aspect_ratio=increase,'
             'crop=1080:1920,setsar=1',
        '-c:v','libx264','-preset','fast','-crf','23',
        '-c:a','aac', out
    ], check=True)
    return out

# ------------------ Main Flow ------------------
def process_subreddit(reddit, drive, folder_id, subreddit, remaining):
    processed = 0
    for post in reddit.subreddit(subreddit).top(time_filter="day", limit=50):
        if remaining - processed <= 0:
            break
        if not any(d in post.url for d in VIDEO_DOMAINS):
            continue

        video, dur = download_video(post.url)
        if not video or not (10 <= dur <= 180):
            continue

        vert = convert_to_tiktok(video)
        os.remove(video)

        safe = sanitize_filename(post.title)
        final = f"{safe}.mp4"
        os.rename(vert, final)

        upload_to_drive(drive, folder_id, final)
        os.remove(final)

        processed += 1
        print(f"✅ [{subreddit}] Processed: {safe}")

    return processed

if __name__ == "__main__":
    # set up
    reddit = praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        user_agent="script:golfbot:v1.0"
    )
    drive = authenticate_drive()
    folder_id = get_or_create_folder(drive, FOLDER_NAME)

    # process primary
    total = process_subreddit(reddit, drive, folder_id, SUBREDDIT_PRIMARY, TARGET_COUNT)
    # fallback if needed
    if total < TARGET_COUNT:
        needed = TARGET_COUNT - total
        print(f"Only got {total} from r/{SUBREDDIT_PRIMARY}, fetching {needed} from r/{SUBREDDIT_FALLBACK}")
        process_subreddit(reddit, drive, folder_id, SUBREDDIT_FALLBACK, needed)
