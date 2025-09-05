import os
import re
import subprocess
import json
import random
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
        w, h = proc.stdout.strip().split('x')
        return int(w), int(h)
    return None, None

def get_true_duration(path):
    """Return video duration in seconds using ffprobe."""
    try:
        proc = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', path
        ], capture_output=True, text=True)
        duration = float(proc.stdout.strip())
        return duration
    except Exception:
        return 0

# ------------------ Video Processing ------------------

VIDEO_DOMAINS = {
    'reddit.com','v.redd.it','youtube.com','youtu.be',
    'streamable.com','gfycat.com','imgur.com','tiktok.com',
    'instagram.com','twitter.com','x.com','twitch.tv',
    'dailymotion.com','rumble.com'
}

TEAM_COLORS = {
    "Lakers": "#552583",
    "Celtics": "#007A33",
    "Heat": "#98002E",
    "Knicks": "#F58426",
    "Bulls": "#CE1141",
    "Warriors": "#1D428A",
    "Nets": "#000000",
    "Suns": "#E56020",
    "76ers": "#006BB6",
    "Bucks": "#00471B"
    # Add/modify as needed
}

WATERMARK_POSITIONS = [
    ("30", "60"),      # top left
    ("830", "50"),     # top right
    ("30", "1800"),    # bottom left
    ("830", "1800"),   # bottom right
    ("450", "1650"),   # center bottom
    ("450", "100"),    # center top
]

def pick_background_type():
    return random.choice(['black', 'blur', 'teamcolor'])

def get_team_from_title(title):
    for team in TEAM_COLORS:
        if team.lower() in title.lower():
            return team
    return None

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
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
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

def process_video_with_background(input_mp4, output_mp4, mode, post_title, team_color=None):
    filter_vf = ""
    if mode == "black":
        filter_vf = "scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    elif mode == "blur":
        filter_vf = (
            "split=2[main][bg];"
            "[bg]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920,boxblur=20[bg2];"
            "[main]scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2[main2];"
            "[bg2][main2]overlay=(W-w)/2:(H-h)/2"
        )
    elif mode == "teamcolor" and team_color:
        filter_vf = f"color=size=1080x1920:color={team_color}[bg];[0]scale=1080:-1:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2[main2];[bg][main2]overlay=(W-w)/2:(H-h)/2"

    # Watermark animation setup (moves position every 7 seconds)
    drawtext_filters = ""
    try:
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_mp4
        ], capture_output=True, text=True)
        duration = float(proc.stdout.strip())
    except Exception:
        duration = 30
    n_locs = len(WATERMARK_POSITIONS)
    interval = 7
    for i, (x, y) in enumerate(WATERMARK_POSITIONS):
        start = i * interval
        end = min((i + 1) * interval, int(duration))
        draw = (
            f"drawtext=text='@impulseprod':fontcolor=white:fontsize=60:x={x}:y={y}:"
            f"box=1:boxborderw=6:boxcolor=black@0.1:enable='between(t,{start},{end})',"
        )
        drawtext_filters += draw
    full_filter = filter_vf + "," + drawtext_filters[:-1]  # Remove trailing comma
    cmd = [
        'ffmpeg','-y','-i',input_mp4,
        '-vf',full_filter,
        '-c:v','libx264','-preset','fast','-crf','23',
        '-c:a','aac',output_mp4
    ]
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"❌ Processing failed: {e}")

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
        if not path:
            continue
        # Double check and enforce duration using the actual downloaded file
        true_dur = get_true_duration(path)
        if not (10 <= true_dur <= 180):
            os.remove(path)
            continue

        # Pick background style for THIS video
        bg_mode = pick_background_type()
        team = get_team_from_title(post.title)
        team_color = TEAM_COLORS.get(team, "#000000")
        # Compose output name
        base_out = os.path.splitext(path)[0]
        final_vid = f"{base_out}_VERTICAL.mp4"

        process_video_with_background(
            input_mp4=path,
            output_mp4=final_vid,
            mode=bg_mode if bg_mode != "teamcolor" else "teamcolor",
            post_title=post.title,
            team_color=team_color
        )

        os.remove(path)
        headline = sanitize_filename(generate_headline(post.title))
        final = f"{headline}.mp4"
        os.rename(final_vid, final)
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
