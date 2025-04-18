# poly.py
import os
import sys
import json
import shutil
import uuid
import logging
import subprocess
import datetime as dt
from tempfile import TemporaryDirectory

import requests
from snscrape.modules.twitter import TwitterSearchScraper

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─────────────────────────── CONFIG ────────────────────────────
ACCOUNTS           = ["disclosetv", "CollinRugg", "MarioNawfal"]
MODEL              = "google/gemini-2.0-flash-lite-001"
HEADLINE_MAX_WORDS = 10
MIN_DURATION       = 10        # seconds
MAX_DURATION       = 180       # seconds
OUTPUT_RES_W       = 1080
OUTPUT_RES_H       = 1920
FOLDER_NAME        = "Poly"
COOKIES_FILE       = "cookies.txt"
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
# ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s",
    stream=sys.stdout,
)

API_KEY  = os.getenv("OPENROUTER_API_KEY")
SA_JSON  = os.getenv("GDRIVE_SERVICE_ACCOUNT")

if not API_KEY or not SA_JSON:
    logging.error("Missing required secrets; aborting.")
    sys.exit(1)

# write service‑account JSON to temp file
sa_path = os.path.join("/tmp", f"sa-{uuid.uuid4()}.json")
with open(sa_path, "w", encoding="utf-8") as f:
    f.write(SA_JSON)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
creds  = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
drive  = build("drive", "v3", credentials=creds, cache_discovery=False)

def ensure_drive_folder(name: str) -> str:
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive.files().list(q=query, fields="files(id,name)", pageSize=1).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    file_metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    folder = drive.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]

FOLDER_ID = ensure_drive_folder(FOLDER_NAME)

def openrouter_chat(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": prompt.strip()}
        ],
        "max_tokens": 20,
        "temperature": 0.2,
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def score_tweet(text: str) -> float:
    prompt = (
        "You are scoring tweets for relevance to today's U.S. political news. "
        "Return a single number from 0–10 (0 = violent content or merely about a controversial "
        "figure with no news value, 10 = extremely relevant breaking U.S. political news). "
        f"Tweet:\n\"{text}\""
    )
    try:
        score_str = openrouter_chat(prompt)
        return float(score_str.split()[0])
    except Exception as e:
        logging.warning("Scoring failed: %s", e)
        return 0.0

def headline_from(text: str) -> str:
    prompt = (
        "Create a concise headline (under 10 words, no hashtags, title case) for this tweet video:\n"
        f"{text}"
    )
    headline = openrouter_chat(prompt)
    headline = headline.replace("/", "-")[:60]
    return headline

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

def download_video(url: str, outdir: str) -> str | None:
    name = str(uuid.uuid4())
    out_tpl = os.path.join(outdir, f"{name}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--cookies", COOKIES_FILE,
        "--force-ipv4",
        "-o", out_tpl,
        url,
    ]
    res = run(cmd)
    if res.returncode != 0:
        logging.warning("yt‑dlp failed: %s", res.stderr.splitlines()[-1] if res.stderr else res.stderr)
        return None
    # find the downloaded file
    for ext in ("mp4", "mkv", "webm", "mov"):
        fpath = os.path.join(outdir, f"{name}.{ext}")
        if os.path.exists(fpath):
            return fpath
    return None

def validate_video(path: str) -> bool:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path
    ]
    res = run(cmd)
    if res.returncode != 0:
        return False
    info = json.loads(res.stdout)
    duration = float(info["format"]["duration"])
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        return False
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return has_audio

def convert_to_portrait(src: str, dst: str) -> bool:
    vf = (
        f"scale={OUTPUT_RES_W}:{OUTPUT_RES_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_RES_W}:{OUTPUT_RES_H}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        dst
    ]
    res = run(cmd)
    return res.returncode == 0

def upload_to_drive(path: str, name_on_drive: str) -> bool:
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=False)
    body = {"name": name_on_drive, "parents": [FOLDER_ID]}
    try:
        drive.files().create(media_body=media, body=body, fields="id").execute()
        return True
    except Exception as e:
        logging.warning("Drive upload failed: %s", e)
        return False

def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).rstrip()

def gather_tweets() -> list[dict]:
    today   = dt.datetime.utcnow().date()
    yday    = today - dt.timedelta(days=1)
    since   = yday.strftime("%Y-%m-%d")
    until   = today.strftime("%Y-%m-%d")
    tweets  = []

    for acct in ACCOUNTS:
        q = f"from:{acct} since:{since} until:{until} filter:videos"
        logging.info("Scraping %s", q)
        try:
            for tw in TwitterSearchScraper(q).get_items():
                tweets.append(
                    {"url": tw.url, "text": tw.content.replace("\n", " ")}
                )
        except Exception as e:
            logging.warning("Scraping failed for %s: %s", acct, e)

    logging.info("Collected %d candidate tweets", len(tweets))
    return tweets

def main() -> None:
    tweets = gather_tweets()
    for t in tweets:
        t["score"] = score_tweet(t["text"])
    tweets.sort(key=lambda x: x["score"], reverse=True)

    uploaded = 0
    with TemporaryDirectory(prefix="poly_") as workdir:
        for tw in tweets:
            if uploaded >= 5:
                break
            logging.info("Processing tweet %s (score %.1f)", tw["url"], tw["score"])

            raw_path = download_video(tw["url"], workdir)
            if not raw_path:
                continue
            if not validate_video(raw_path):
                logging.info("Video failed validation")
                continue

            headline = sanitize_filename(headline_from(tw["text"]))
            final_mp4 = os.path.join(workdir, f"{headline}.mp4")

            if not convert_to_portrait(raw_path, final_mp4):
                logging.info("FFmpeg conversion failed")
                continue

            if upload_to_drive(final_mp4, f"{headline}.mp4"):
                uploaded += 1
                logging.info("Uploaded: %s", headline)
            else:
                logging.info("Upload failed")

    logging.info("Uploaded %d videos", uploaded)

if __name__ == "__main__":
    try:
        main()
    finally:
        # Clean tmp service‑account file
        if os.path.exists(sa_path):
            os.remove(sa_path)
