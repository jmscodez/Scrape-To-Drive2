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

# ------------------ Utilities ------------------
def sanitize_filename(fn):
    fn = re.sub(r'[\\/*?:"<>|]', "", fn)
    return fn.strip()[:100]
def get_video_resolution(path):
    """Return (width, height) of the first video stream via ffprobe."""
    cmd = [
        'ffprobe','-v','error',
        '-select_streams','v:0',
        '-show_entries','stream=width,height',
        '-of','csv=s=x:p=0',
        path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and 'x' in proc.stdout:
        w,h = proc.stdout.strip().split('x')
        return int(w), int(h)
    return None, None

# ------------------ Video Download & Processing ------------------
VIDEO_DOMAINS = {
    'reddit.com','v.redd.it','youtube.com','youtu.be',
    'streamable.com','gfycat.com','imgur.com','tiktok.com',
    'instagram.com','twitter.com','x.com','twitch.tv',
    'dailymotion.com','rumble.com'
}
def download_video(url):
    ydl_opts = {
        'outtmpl': '%(id)s.%(ext)s',
        'format': 'bestvideo[height<=1080]+bestaudio/best/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.reddit.com/'
        },
        'extractor_args': {'reddit': {'skip_auth': True}}
    }
    # add universal cookies.txt support if you want to enable for e.g. Twitter/Instagram
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            # verify audio
            result = subprocess.run(
                ['ffprobe','-v','error','-select_streams','a',
                 '-show_entries','stream=codec_type','-of','csv=p=0', fn],
                stdout=subprocess.PIPE, text=True
            )
            if 'audio' not in result.stdout:
                os.remove(fn)
                return None, 0
            return fn, info.get('duration',0)
    except Exception as e:
        print(f"❌ Download failed for {url}: {e}")
        return None, 0

def convert_to_tiktok(video_path):
    """If aspect ratio ≈9:16: scale & crop.
    Else: crop centered square, blur it to 1080x1920, overlay square."""
    width, height = get_video_resolution(video_path)
    if not width or not height:
        print("⚠️ Could not get resolution; defaulting to simple crop")
        method = 'simple'
    else:
        aspect = width/height
        method = 'simple' if abs(aspect - 9/16) < 0.02 else 'blur'
    output = video_path.replace(".mp4","_VERTICAL.mp4")
    if method == 'simple':
        # simple scale+crop to 1080x1920
        cmd = [
            'ffmpeg','-i',video_path,
            '-vf','scale=1080:1920:force_original_aspect_ratio=increase,'
                  'crop=1080:1920,setsar=1',
            '-c:v','libx264','-preset','fast','-crf','23',
            '-c:a','aac','-y', output
        ]
    else:
        # square size = min(width,height)
        sq = min(width, height)
        x_off = (width - sq)/2
        y_off = (height - sq)/2
        # filter: split into two streams, one blurred background, one FG square
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
            '-c:a','aac','-y', output
        ]
    try:
        subprocess.run(cmd, check=True)
        return output
    except Exception as e:
        print(f"❌ Conversion failed ({method}): {e}")
        return None

# ------------------ Headline Generation ------------------
def generate_headline(post_title):
    try:
        # Truncate the title to avoid hitting token limits
        truncated_title = post_title[:200]
        prompt = (
            "Your job is to take captions that I give you and turn them into a headline that would be used on a TikTok video. "
            "The output should ONLY be the new title. Rules:\n"
            "1. It should be text at the top/bottom that explains what's happening in the video.\n"
            "2. It should be tailored for TikTok SEO.\n"
            "3. Remove any '_VERTICAL.mp4' text if present.\n"
            "4. Use a max of 2 emojis.\n"
            "5. NO HASHTAGS.\n"
            "6. If a name is included, keep the name in the caption.\n"
            "\nInput: '[Highlight] Cam Ward throws a dime at Miami Pro Day_VERTICAL'\n"
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
        caption = response.json()['choices'][0]['message']['content'].strip()
        caption = re.sub(r'_VERTICAL\.mp4', '', caption) # Remove any remaining _VERTICAL.mp4
        caption = re.sub(r'#\w+', '', caption) # Remove any hashtags
        return caption[:150]
    except Exception as e:
        print(f"⚠️ Headline generation failed: {str(e)}")
        return sanitize_filename(post_title)[:100] # Fallback

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0"
)
if __name__ == "__main__":
    drive = authenticate_drive()
    folder_id = get_or_create_folder(drive, "Impulse")
    processed = 0
    for post in reddit.subreddit("NBA").top(time_filter="day", limit=50):
        if processed >= 3:
            break
        if not any(d in post.url for d in VIDEO_DOMAINS):
            continue
        path, dur = download_video(post.url)
        if not path or not (10 <= dur <= 180):
            continue
        vert = convert_to_tiktok(path)
        os.remove(path)
        if not vert:
            continue
        headline = sanitize_filename(generate_headline(post.title))
        final = f"{headline}.mp4"
        os.rename(vert, final)
        upload_to_drive(drive, folder_id, final)
        # --- Add data to Google Sheet ---
        try:
            add_video_to_sheet(
                source="NBA",
                reddit_url=post.url,
                reddit_caption=post.title,
                drive_video_name=headline
            )
        except Exception as e:
            print(f"⚠️ Failed to add data to Google Sheet: {e}")
        os.remove(final)
        processed += 1
        print(f"✅ Processed: {headline}")
