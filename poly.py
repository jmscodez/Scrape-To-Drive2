import os
import re
import time
import json
import datetime
import subprocess
import requests
import textwrap

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------------ ChromeDriver Setup ------------------
chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/chromium-browser")
driver = webdriver.Chrome(
    executable_path=os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"),
    options=chrome_options
)

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
    resp = drive_service.files().list(q=query, fields="files(id)").execute()
    items = resp.get('files', [])
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

# ------------------ Helpers ------------------
def sanitize_filename(fn):
    fn = re.sub(r'[\\/*?:"<>|]', "", fn)
    return fn.strip()[:100]

def get_video_resolution(path):
    proc = subprocess.run([
        'ffprobe','-v','error',
        '-select_streams','v:0',
        '-show_entries','stream=width,height',
        '-of','csv=s=x:p=0',
        path
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        'http_headers': {'User-Agent':'Mozilla/5.0'}
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

def convert_to_9_16_centered(video_path):
    out = video_path.replace(".mp4", "_9_16.mp4")
    cmd = [
        'ffmpeg','-y','-i',video_path,
        '-vf',
        'scale=1080:1920:force_original_aspect_ratio=decrease,'
        'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black',
        '-c:v','libx264','-preset','fast','-crf','23',
        '-c:a','copy',
        out
    ]
    subprocess.run(cmd, check=True)
    return out

# ------------------ Caption Generation ------------------
def generate_short_title(text):
    prompt = (
        "From the following text, create a concise TikTok-style video title (≤10 words, no hashtags or special chars).\n"
        f"Text: {text}"
    )
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        },
        json={
            "model":"google/gemini-2.0-flash-lite-001",
            "messages":[
                {"role":"system","content":"You write concise video titles."},
                {"role":"user","content":prompt}
            ],
            "max_tokens":50,
            "temperature":0.7
        }
    )
    resp.raise_for_status()
    title = resp.json()['choices'][0]['message']['content'].strip()
    title = re.sub(r"[^\w\s.,!?'-]","", title)
    return title[:100]

# ------------------ Tweet Scraping ------------------
def parse_tweet_id(url):
    return url.rstrip('/').split('/')[-1]

def tweet_has_video(url):
    driver.get(url)
    try:
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,'video')))
        return True
    except:
        return False

def get_tweet_text(url):
    driver.get(url)
    try:
        elm = WebDriverWait(driver,10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,"article [data-testid='tweetText']"))
        )
        return elm.text
    except:
        return "Breaking news"

# ------------------ Main ------------------
def main():
    drive = authenticate_drive()
    folder_id = get_or_create_folder(drive, "Poly")

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    users = ["disclosetv","CollinRugg","MarioNawfal"]
    seen = set()
    collected = []

    # collect URLs & texts
    for u in users:
        search = f"https://twitter.com/search?q=from%3A{u}%20filter%3Avideos%20since%3A{yesterday}&src=typed_query"
        driver.get(search)
        time.sleep(2)
        for a in driver.find_elements(By.CSS_SELECTOR,"a[href*='/status/']"):
            url = a.get_attribute("href")
            if url not in seen:
                seen.add(url)
                text = get_tweet_text(url)
                collected.append((url, text))

    # process up to 5
    processed = 0
    for url, text in collected:
        if processed >= 5: break
        if not tweet_has_video(url): continue
        vid, dur = download_video(url)
        if not vid or not (10 <= dur <= 180): continue

        t9 = convert_to_9_16_centered(vid); os.remove(vid)
        title = sanitize_filename(generate_short_title(text))
        final = f"{title}.mp4"
        os.rename(t9, final)

        upload_to_drive(drive, folder_id, final)
        os.remove(final)

        processed += 1
        print(f"✅ Processed: {final}")

if __name__=="__main__":
    try:
        main()
    finally:
        driver.quit()
