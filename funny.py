import os
import re
import subprocess
import json
import praw
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------------ Google Drive Integration ------------------
SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])

def authenticate_drive():
    creds = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)

def get_or_create_folder(drive_service, folder_name):
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive_service.files().list(q=query, fields="files(id)").execute()
    items = res.get('files', [])
    if items:
        return items[0]['id']
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

def sanitize_filename(fn):
    fn = re.sub(r'[\\/*?:"<>|]', "", fn)
    return fn.strip()[:100]

def download_video(url):
    ydl_opts = {
        'outtmpl': '%(id)s.%(ext)s',
        'format': 'bestvideo[height<=1080]+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'cookiefile': 'cookies.txt',
        'force_ipv4': True,
        'http_headers': {'User-Agent':'Mozilla/5.0'}
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
        # verify audio track
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
    output_path = video_path.replace(".mp4", "_VERTICAL.mp4")
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-y', output_path
    ]
    subprocess.run(cmd, check=True)
    return output_path

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:funnybot:v1.0 (by /u/YourUsername)"
)

if __name__ == "__main__":
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "funny")
    processed = 0
    target = 3  # how many videos per run

    for post in reddit.subreddit("funny").top(time_filter="day", limit=50):
        if processed >= target:
            break

        # skip non-video posts
        if not any(domain in post.url for domain in VIDEO_DOMAINS):
            continue

        # download
        video_file, duration = download_video(post.url)
        if not video_file or not (10 <= duration <= 180):
            continue

        # convert to vertical
        vertical_file = convert_to_tiktok(video_file)
        os.remove(video_file)

        # use post title as filename
        safe_title = sanitize_filename(post.title)
        final_name = f"{safe_title}.mp4"
        os.rename(vertical_file, final_name)

        # upload and cleanup
        upload_to_drive(drive_service, folder_id, final_name)
        os.remove(final_name)

        processed += 1
        print(f"✅ Processed: {safe_title}")

    print(f"\n🎉 Completed: {processed}/{target} videos processed")
