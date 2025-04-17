# poly.py

# ── DISABLE SSL VERIFICATION FOR requests (and thus snscrape) ────────────────
import requests, ssl
requests.packages.urllib3.disable_warnings()  # suppress warnings
_orig_request = requests.api.request
def _request_no_verify(method, url, **kwargs):
    kwargs['verify'] = False
    return _orig_request(method, url, **kwargs)
requests.api.request = _request_no_verify

# ── STANDARD IMPORTS ───────────────────────────────────────────────────────────
import os
import sys
import json
import re
import tempfile
import datetime
import subprocess
from pathlib import Path

from yt_dlp import YoutubeDL
from snscrape.modules.twitter import TwitterSearchScraper
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ACCOUNTS          = ["disclosetv", "CollinRugg", "MarioNawfal"]
MAX_TO_UPLOAD     = 5
MIN_DURATION      = 10    # seconds
MAX_DURATION      = 180   # seconds
OUTPUT_RESOLUTION = (1080, 1920)
DRIVE_FOLDER_NAME = "Poly"
OPENROUTER_URL    = "https://api.openrouter.ai/v1/chat/completions"
OPENROUTER_MODEL  = "google/gemini-2.0-flash-lite-001"
COOKIEFILE        = "cookies.txt"

# ── HELPERS ────────────────────────────────────────────────────────────────────
def get_date_ranges():
    today     = datetime.datetime.utcnow().date()
    yesterday = today - datetime.timedelta(days=1)
    return yesterday.isoformat(), today.isoformat()

def fetch_tweets():
    since, until = get_date_ranges()
    out = []
    for acct in ACCOUNTS:
        query = f"from:{acct} since:{since} until:{until} filter:videos"
        try:
            for t in TwitterSearchScraper(query).get_items():
                out.append({"url": t.url, "text": t.content})
        except Exception as e:
            print(f"⚠️ snscrape error for {acct}: {e}", file=sys.stderr)
    return out

def score_tweet(text, api_key):
    prompt = (
        "Identify the top U.S. news stories today. Then score this tweet "
        "from 1 to 10 for its relevance to today's top U.S. political news. "
        "Give 0 if it contains violent content or is only about a controversial person "
        "(e.g. Andrew Tate). You may include Trump, Musk, or other leaders if relevant. "
        "Here is the tweet: \"" + text + "\". Only output a number."
    )
    r = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": OPENROUTER_MODEL, "messages":[{"role":"user","content":prompt}]},
        timeout=30
    )
    r.raise_for_status()
    s = r.json()["choices"][0]["message"]["content"].strip()
    try:
        return float(re.match(r"\d+(\.\d+)?", s).group())
    except:
        return 0.0

def download_video(url, dl_dir):
    opts = {
        "format": "bv+ba/bestaudio/best",
        "cookiefile": COOKIEFILE,
        "forceipv4": True,
        "outtmpl": str(dl_dir / "%(id)s.%(ext)s"),
        "quiet": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return dl_dir / f"{info['id']}.{info['ext']}"

def probe_video(path):
    has_audio = bool(subprocess.run(
        ["ffprobe","-v","error","-select_streams","a",
         "-show_entries","stream=codec_type",
         "-of","default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True
    ).stdout.strip())
    dur = float(subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True
    ).stdout.strip())
    return has_audio, dur

def convert_to_portrait(src, dst):
    w, h = OUTPUT_RESOLUTION
    vf = f"scale={w}:-2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    subprocess.run(
        ["ffmpeg","-y","-i",str(src),"-vf",vf,
         "-c:v","libx264","-c:a","copy",str(dst)],
        check=True
    )

def get_drive_service():
    info  = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive","v3", credentials=creds)

def ensure_folder(drive, name):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
    if res:
        return res[0]["id"]
    f = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}
    ).execute()
    return f["id"]

def upload_file(drive, folder_id, path):
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    meta  = {"name": path.name, "parents": [folder_id]}
    drive.files().create(body=meta, media_body=media, fields="id").execute()

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    if "OPENROUTER_API_KEY" not in os.environ or "GDRIVE_SERVICE_ACCOUNT" not in os.environ:
        print("❗ Missing OPENROUTER_API_KEY or GDRIVE_SERVICE_ACCOUNT", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ["OPENROUTER_API_KEY"]
    drive   = get_drive_service()
    folder  = ensure_folder(drive, DRIVE_FOLDER_NAME)

    all_tw = fetch_tweets()
    if not all_tw:
        print("No video tweets found.")
        return

    scored = [(t["url"], t["text"], score_tweet(t["text"], api_key)) for t in all_tw]
    scored.sort(key=lambda x: x[2], reverse=True)

    workdir = Path(tempfile.mkdtemp(prefix="poly_"))
    dl, pr = workdir / "downloads", workdir / "processed"
    dl.mkdir(); pr.mkdir()

    uploaded = 0
    for url, text, score in scored:
        if uploaded >= MAX_TO_UPLOAD:
            break
        print(f"\n➡️ Processing {url} (score {score:.1f})")
        try:
            raw = download_video(url, dl)
            has_audio, dur = probe_video(raw)
            if not has_audio or dur < MIN_DURATION or dur > MAX_DURATION:
                raw.unlink()
                continue

            tmp   = pr / f"{raw.stem}_c.mp4"
            convert_to_portrait(raw, tmp)
            raw.unlink()

            hl = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": OPENROUTER_MODEL,
                    "messages":[{"role":"user","content":
                        f"Generate a concise headline (≤10 words, no hashtags or special chars) for this video tweet: {url}"
                    }],
                },
                timeout=30
            ).json()["choices"][0]["message"]["content"]

            safe = re.sub(r"[^A-Za-z0-9 _-]", "", hl).strip()
            safe = "_".join(safe.split()) or "video"
            final = pr / f"{safe}.mp4"
            tmp.rename(final)

            upload_file(drive, folder, final)
            final.unlink()
            print(f"✅ Uploaded {safe}.mp4")
            uploaded += 1

        except Exception as e:
            print(f"⚠️ skip {url}: {e}", file=sys.stderr)

    # Cleanup
    for d in (dl, pr):
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    workdir.rmdir()
    print(f"\nFinished. {uploaded} videos uploaded.")

if __name__ == "__main__":
    main()
