import os
import re
import subprocess
import json
import random
import praw
import yt_dlp
import requests
import logging
import time
from contextlib import contextmanager
from functools import wraps
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sheets_client import add_video_to_sheet

# ------------------ Logging Setup ------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('nba_processor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------ Environment Variable Validation ------------------
REQUIRED_ENV_VARS = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'OPENROUTER_API_KEY',
    'GDRIVE_SERVICE_ACCOUNT'
]
missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
if missing:
    raise ValueError(f"Missing required environment variables: {missing}")

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
    try:
        drive_service.files().create(
            body={'name': name, 'parents': [folder_id]},
            media_body=media
        ).execute()
        print(f"Uploaded {name} to Google Drive")
    except Exception as e:
        print(f"❌ Google Drive upload failed: {e}")
        raise

# ------------------ Utility Functions ------------------
def sanitize_filename(fn):
    fn = re.sub(r'[\/*?:"<>|]', "", fn)
    return fn.strip()[:100]

def get_true_duration(path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of',
             'default=noprint_wrappers=1:nokey=1', path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"ffprobe error on {path}: {e}")
        return 0

def get_video_resolution(path):
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=s=x:p=0',
        path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and 'x' in proc.stdout:
        w, h = proc.stdout.strip().split('x')
        return int(w), int(h)
    return None, None

@contextmanager
def temporary_files(*file_paths):
    try:
        yield
    finally:
        for path in file_paths:
            if os.path.exists(path):
                os.remove(path)

def safe_cleanup(*file_paths):
    for path in file_paths:
        if path and os.path.exists(path):
            os.remove(path)

def retry_with_backoff(max_retries=3, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    print(f"Attempt {attempt+1}: {e}, retrying...")
                    if attempt == max_retries - 1:
                        print(f"Exceeded maximum retries for {func.__name__}")
                        raise
                    time.sleep(2 ** attempt)
            return None
        return wrapper
    return decorator

# ------------------ Video Download & Processing ------------------
VIDEO_DOMAINS = {
    'reddit.com','v.redd.it','youtube.com','youtu.be',
    'streamable.com','gfycat.com','imgur.com','tiktok.com',
    'instagram.com','twitter.com','x.com','twitch.tv',
    'dailymotion.com','rumble.com'
}

def pick_background_type():
    return random.choice(['black', 'blur'])

@retry_with_backoff(max_retries=3)
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
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            # verify audio
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', fn],
                stdout=subprocess.PIPE, text=True
            )
            if 'audio' not in result.stdout:
                os.remove(fn)
                return None
            return fn
    except Exception as e:
        print(f"❌ Download failed for {url}: {e}")
    return None

MAX_PROCESS_SECONDS = 600  # 10 minutes in seconds

def process_video_with_background(input_mp4, output_mp4, mode):
    print(f"🎨 Processing with background mode: {mode}")
    
    if mode == "black":
        filter_vf = "scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    elif mode == "blur":
        # Get video resolution for blur method
        width, height = get_video_resolution(input_mp4)
        if not width or not height:
            print("⚠️ Could not get resolution; falling back to black bars")
            filter_vf = "scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
        else:
            # Use the exact blur method from the attached file
            sq = min(width, height)
            x_off = (width - sq) // 2
            y_off = (height - sq) // 2
            filter_vf = (
                f"split=2[bgsrc][fgsrc];"
                f"[bgsrc]crop={sq}:{sq}:{x_off}:{y_off},"
                f"scale=1080:1920,setsar=1,gblur=sigma=20[bg];"
                f"[fgsrc]crop={sq}:{sq}:{x_off}:{y_off},"
                f"scale=1080:1080,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1"
            )
    else:
        # Fallback to black bars
        print(f"⚠️ Warning: Unknown mode '{mode}', falling back to black bars")
        filter_vf = "scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    
    cmd = [
        'ffmpeg', '-y', '-i', input_mp4,
        '-vf', filter_vf,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', output_mp4
    ]
    
    print(f"🔧 Running FFmpeg command with filter...")
    
    try:
        subprocess.run(cmd, check=True, timeout=MAX_PROCESS_SECONDS)
        print(f"✅ Successfully processed video with {mode} background")
    except subprocess.TimeoutExpired:
        print(f"🛑 TIMEOUT: ffmpeg took too long (>{MAX_PROCESS_SECONDS}s)! Deleting files and skipping this video.")
        safe_cleanup(input_mp4, output_mp4)
        raise
    except Exception as e:
        print(f"❌ Processing failed: {e}")
        safe_cleanup(output_mp4)
        raise

def generate_headline(post_title):
    try:
        truncated_title = post_title[:200]
        prompt = (
            "Rewrite the following Reddit NBA highlight as a short, catchy, viral TikTok caption.\n"
            "Rules:\n"
            "- Use at most 2 relevant emojis.\n"
            "- No hashtags anywhere.\n"
            "- Keep the caption under 200 characters.\n"
            "- Make the caption short, natural, and exciting—summarize the moment.\n"
            "- Output ONLY the TikTok caption, and nothing else (no intro, formatting, or explanation).\n\n"
            f"Reddit title:\n{truncated_title}\n\n"
            "TikTok caption:"
        )
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "messages": [{
                "role": "system",
                "content": (
                    "You are a social media expert specializing in creating viral, concise TikTok captions from NBA highlight titles. "
                    "Always obey all instructions precisely and never go over 200 characters."
                )
            }, {
                "role": "user",
                "content": prompt
            }],
            "max_tokens": 500,
            "temperature": 0.85
        }
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content'].strip()
        caption = content.split('\n')[0].replace('_VERTICAL.mp4', '')
        caption = re.sub(r'#\w+', '', caption)
        caption = caption.strip()
        if len(caption) > 200:
            caption = caption[:197] + "..."
        return caption
    except Exception as e:
        print(f"⚠️ Headline generation failed: {str(e)}")
        return sanitize_filename(post_title)[:100]

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0"
)

MAX_VIDEOS = 3
MAX_POSTS = 20  # Lowered for efficiency
MIN_SECONDS = 10
MAX_SECONDS = 180

if __name__ == "__main__":
    drive = authenticate_drive()
    folder_id = get_or_create_folder(drive, "Impulse")
    processed = 0

    # Pre-filter posts before running expensive video downloads
    print("Fetching new posts from r/NBA...")
    posts = [
        post for post in reddit.subreddit("NBA").top(time_filter="day", limit=MAX_POSTS)
        if any(d in post.url for d in VIDEO_DOMAINS)
    ]
    print(f"Found {len(posts)} potential posts with video URLs.")

    for post in posts:
        if processed >= MAX_VIDEOS:
            break

        print(f"Examining post: {post.title[:60]} | URL: {post.url}")

        # Download video with backoff/retries
        path = download_video(post.url)
        if not path or not os.path.isfile(path):
            print(f"⏭️ SKIP: Could not download video for {post.url}")
            continue

        true_dur = get_true_duration(path)
        print(f"🕒 CHECK: Downloaded video duration = {true_dur:.2f} sec for post '{post.title[:60]}'")
        if not (MIN_SECONDS <= true_dur <= MAX_SECONDS):
            print(f"⏭️ SKIP: Removing video '{path}' with duration {true_dur:.2f} sec (⛔ not in range {MIN_SECONDS}-{MAX_SECONDS}s).")
            safe_cleanup(path)
            continue

        print(f"✅ PROCESS: {post.url} (duration={true_dur:.2f}s, proceeding!)")
        bg_mode = pick_background_type()
        
        print(f"🎲 Selected background mode: {bg_mode}")
        
        final_vid = path.replace(".mp4", "_VERTICAL.mp4")

        try:
            process_video_with_background(
                input_mp4=path,
                output_mp4=final_vid,
                mode=bg_mode
            )
        except subprocess.TimeoutExpired:
            print(f"⏭️ SKIP: Video processing killed due to excess runtime. Removing and proceeding to next video.")
            safe_cleanup(path, final_vid)
            continue
        except Exception as e:
            print(f"⏭️ SKIP: Video processing failed. {e}")
            safe_cleanup(path, final_vid)
            continue

        safe_cleanup(path)

        headline = sanitize_filename(generate_headline(post.title))
        final = f"{headline}.mp4"
        try:
            os.rename(final_vid, final)
        except Exception as e:
            print(f"⚠️ Rename failed: {e}")
            safe_cleanup(final_vid)
            continue

        # Attempt upload and logging; ensure cleanup on failure
        try:
            upload_to_drive(drive, folder_id, final)
            add_video_to_sheet(
                source="NBA",
                reddit_url=post.url,
                reddit_caption=post.title,
                drive_video_name=headline
            )
        except Exception as e:
            print(f"⚠️ Failed to add data to Google Sheet or upload to Drive: {e}")
            safe_cleanup(final)
            continue

        safe_cleanup(final)
        processed += 1
        print(f"✅ Processed: {headline}")

    print("All done, finished scanning posts!")
