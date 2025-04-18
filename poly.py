#!/usr/bin/env python3
import os, sys, uuid, json, logging, subprocess
import datetime as dt
from tempfile import TemporaryDirectory

import requests, certifi, ssl
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─── SSL / certifi fix ─────────────────────────────────────────
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["SSL_CERT_FILE"]     = certifi.where()
ssl._create_default_https_context = ssl._create_unverified_context

# ─── CONFIG ───────────────────────────────────────────────────
ACCOUNTS       = ["disclosetv", "CollinRugg", "MarioNawfal"]
MODEL          = "google/gemini-2.0-flash-lite-001"
DATE_FMT       = "%Y-%m-%d"
MIN_DURATION   = 10
MAX_DURATION   = 180
OUTPUT_W       = 1080
OUTPUT_H       = 1920
FOLDER_NAME    = "Poly"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# ─── SECRETS ──────────────────────────────────────────────────
API_KEY = os.getenv("OPENROUTER_API_KEY")
SA_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT")
if not API_KEY or not SA_JSON:
    logging.error("Missing required secrets; aborting.")
    sys.exit(1)

# ─── GDRIVE AUTH ───────────────────────────────────────────────
sa_path = os.path.join("/tmp", f"sa-{uuid.uuid4()}.json")
with open(sa_path, "w") as f:
    f.write(SA_JSON)
creds = service_account.Credentials.from_service_account_file(
    sa_path, scopes=["https://www.googleapis.com/auth/drive.file"]
)
drive = build("drive", "v3", credentials=creds, cache_discovery=False)

def ensure_drive_folder(name):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()
    return folder["id"]

FOLDER_ID = ensure_drive_folder(FOLDER_NAME)

# ─── TWITTER: GRAPHQL GUEST SCRAPE ────────────────────────────
GUEST_URL = "https://api.twitter.com/1.1/guest/activate.json"
SEARCH_URL = "https://api.twitter.com/2/search/adaptive.json"
# static App Bearer token used by Twitter Web App (subject to change)
APP_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAAANRILgAAAAA%..."
)

def get_guest_token():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.post(GUEST_URL, headers=headers, json={})
    r.raise_for_status()
    return r.json()["guest_token"]

def fetch_tweets(username, since, until, guest_token):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "authorization": APP_BEARER,
        "x-guest-token": guest_token
    }
    q = f"from:{username} filter:videos since:{since} until:{until}"
    params = {"q": q, "count": "100"}
    r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json().get("globalObjects", {})
    tweets = []
    media = data.get("media", {})
    for tid, tw in data.get("tweets", {}).items():
        mids = [m["media_key"] for m in tw.get("extended_entities", {}).get("media", [])]
        if any(media.get(mid, {}).get("type") == "video" for mid in mids):
            url  = f"https://twitter.com/{username}/status/{tid}"
            text = tw.get("full_text", tw.get("text", "")).replace("\n", " ")
            tweets.append({"url": url, "text": text})
    return tweets

# ─── OPENROUTER INTERACTIONS ──────────────────────────────────
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
        "0=violent or merely about a controversial figure, 10=breaking political news. "
        f"Tweet: \"{text}\""
    )
    try:
        return float(openrouter_chat(prompt).split()[0])
    except:
        return 0.0

def headline_from(text):
    prompt = "Create a concise headline (<10 words, no hashtags, title case):\n" + text
    hl = openrouter_chat(prompt)
    return "".join(c for c in hl if c.isalnum() or c in (" ", "-", "_"))[:60].strip()

# ─── VIDEO PROCESSING ────────────────────────────────────────
def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def download_video(url, outdir):
    name = str(uuid.uuid4())
    tpl  = os.path.join(outdir, f"{name}.%(ext)s")
    res  = run(["yt-dlp", "--force-ipv4", "-o", tpl, url])
    if res.returncode != 0:
        return None
    for ext in ("mp4","mkv","webm","mov"):
        p = os.path.join(outdir, f"{name}.{ext}")
        if os.path.exists(p):
            return p
    return None

def validate_video(path):
    res  = run(["ffprobe","-v","quiet","-print_format","json","-show_streams","-show_format",path])
    if res.returncode != 0:
        return False
    info = json.loads(res.stdout)
    dur  = float(info["format"]["duration"])
    if not (MIN_DURATION <= dur <= MAX_DURATION):
        return False
    return any(s.get("codec_type")=="audio" for s in info["streams"])

def convert_to_portrait(src, dst):
    vf = (
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    )
    return run([
        "ffmpeg","-y","-i",src,"-vf",vf,
        "-c:v","libx264","-preset","veryfast","-crf","23","-c:a","copy",dst
    ]).returncode == 0

def upload_to_drive(path, name):
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=False)
    try:
        drive.files().create(media_body=media, body={"name":name,"parents":[FOLDER_ID]}, fields="id").execute()
        return True
    except Exception as e:
        logging.warning("Drive upload failed: %s", e)
        return False

# ─── MAIN ────────────────────────────────────────────────────
def main():
    today = dt.datetime.utcnow().date()
    since = (today - dt.timedelta(days=1)).strftime(DATE_FMT)
    until = today.strftime(DATE_FMT)

    guest = get_guest_token()
    all_tweets = []
    for acct in ACCOUNTS:
        all_tweets.extend(fetch_tweets(acct, since, until, guest))

    for t in all_tweets:
        t["score"] = score_tweet(t["text"])
    all_tweets.sort(key=lambda x: x["score"], reverse=True)

    uploaded = 0
    with TemporaryDirectory(prefix="poly_") as workdir:
        for t in all_tweets:
            if uploaded >= 5:
                break
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
        if os.path.exists(sa_path):
            os.remove(sa_path)
