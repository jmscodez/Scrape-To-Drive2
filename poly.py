# poly.py
import os
import sys
import time
import datetime as dt
import uuid
import json
import logging
import subprocess
from tempfile import TemporaryDirectory

import requests
import certifi
import ssl

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# SSL / certifi
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["SSL_CERT_FILE"]     = certifi.where()
ssl._create_default_https_context = ssl._create_unverified_context

# CONFIG
ACCOUNTS       = ["disclosetv", "CollinRugg", "MarioNawfal"]
MODEL          = "google/gemini-2.0-flash-lite-001"
DATE_FMT       = "%Y-%m-%d"
MIN_DURATION   = 10
MAX_DURATION   = 180
OUTPUT_W       = 1080
OUTPUT_H       = 1920
FOLDER_NAME    = "Poly"
COOKIES_FILE   = "cookies.txt"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# SECRETS
API_KEY = os.getenv("OPENROUTER_API_KEY")
SA_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT")
if not API_KEY or not SA_JSON:
    logging.error("Missing required secrets; aborting.")
    sys.exit(1)

# GDRIVE AUTH
sa_path = os.path.join("/tmp", f"sa-{uuid.uuid4()}.json")
with open(sa_path, "w") as f:
    f.write(SA_JSON)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
creds  = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
drive  = build("drive", "v3", credentials=creds, cache_discovery=False)

def ensure_drive_folder(name):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()
    return folder["id"]

FOLDER_ID = ensure_drive_folder(FOLDER_NAME)

# SELENIUM SETUP
options = Options()
options.binary_location = "/usr/bin/chromium-browser"
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument(
    "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)
driver = webdriver.Chrome(options=options)

def get_tweet_urls_for_user(username, since, max_scrolls=10):
    url = (
        f"https://mobile.twitter.com/search?"
        f"q=from%3A{username}%20filter%3Avideos%20since%3A{since}"
    )
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/status/']"))
        )
    except TimeoutException:
        logging.warning("Timeout loading tweets for %s", username)
    urls = set()
    for _ in range(max_scrolls):
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
        for a in links:
            href = a.get_attribute("href")
            if href and "/status/" in href and "analytics" not in href:
                urls.add(href)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
    return list(urls)

def tweet_has_video(url):
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "video")))
        return True
    except Exception:
        return False

def get_tweet_text(url):
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "article [data-testid='tweetText']"))
        )
        return driver.find_element(By.CSS_SELECTOR, "article [data-testid='tweetText']").text.replace("\n", " ")
    except Exception:
        return "Breaking news"

def openrouter_chat(prompt):
    r = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages":[{"role":"user","content":prompt}], "max_tokens":20, "temperature":0.2},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def score_tweet(text):
    prompt = (
        "Score this tweet for relevance to today's U.S. political news on a scale of 0–10. "
        "0=violent or only about a controversial person, 10=breaking top-tier news. "
        f"Tweet: \"{text}\""
    )
    try:
        return float(openrouter_chat(prompt).split()[0])
    except:
        return 0.0

def headline_from(text):
    prompt = "Create a concise headline (<10 words, no hashtags, title case) for this tweet:\n" + text
    hl = openrouter_chat(prompt)
    return "".join(c for c in hl if c.isalnum() or c in (" ", "-", "_"))[:60].strip()

def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def download_video(url, outdir):
    name = str(uuid.uuid4())
    tpl = os.path.join(outdir, f"{name}.%(ext)s")
    cmd = ["yt-dlp", "--cookies", COOKIES_FILE, "--force-ipv4", "-o", tpl, url]
    res = run(cmd)
    if res.returncode != 0:
        return None
    for ext in ("mp4","mkv","webm","mov"):
        p = os.path.join(outdir, f"{name}.{ext}")
        if os.path.exists(p):
            return p
    return None

def validate_video(path):
    cmd = ["ffprobe","-v","quiet","-print_format","json","-show_streams","-show_format",path]
    res = run(cmd)
    if res.returncode != 0:
        return False
    info = json.loads(res.stdout)
    dur = float(info["format"]["duration"])
    if not (MIN_DURATION <= dur <= MAX_DURATION):
        return False
    return any(s.get("codec_type")=="audio" for s in info["streams"])

def convert_to_portrait(src, dst):
    vf = (
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = ["ffmpeg","-y","-i",src,"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","23","-c:a","copy",dst]
    return run(cmd).returncode == 0

def upload_to_drive(path, name):
    m = MediaFileUpload(path, mimetype="video/mp4", resumable=False)
    try:
        drive.files().create(media_body=m, body={"name":name,"parents":[FOLDER_ID]}, fields="id").execute()
        return True
    except Exception as e:
        logging.warning("Drive upload failed: %s", e)
        return False

def main():
    today = dt.datetime.utcnow().date()
    since = (today - dt.timedelta(days=1)).strftime(DATE_FMT)

    tweets = []
    for acct in ACCOUNTS:
        for url in get_tweet_urls_for_user(acct, since):
            tweets.append({"url":url, "text":get_tweet_text(url)})

    for t in tweets:
        t["score"] = score_tweet(t["text"])
    tweets.sort(key=lambda x: x["score"], reverse=True)

    uploaded = 0
    with TemporaryDirectory(prefix="poly_") as workdir:
        for t in tweets:
            if uploaded >= 5:
                break
            if not tweet_has_video(t["url"]):
                continue
            raw = download_video(t["url"], workdir)
            if not raw or not validate_video(raw):
                continue
            hl = headline_from(t["text"])
            final = os.path.join(workdir, f"{hl}.mp4")
            if not convert_to_portrait(raw, final):
                continue
            if upload_to_drive(final, f"{hl}.mp4"):
                uploaded += 1

    logging.info("Uploaded %d videos", uploaded)

if __name__ == "__main__":
    try:
        main()
    finally:
        driver.quit()
        if os.path.exists(sa_path):
            os.remove(sa_path)
