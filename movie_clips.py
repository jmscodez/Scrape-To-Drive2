import os
import subprocess
import requests
import json
from yt_dlp import YoutubeDL
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sheets_client import add_video_to_sheet

# ── Configuration ──────────────────────────────────────────────────────────────
GDRIVE_SERVICE_ACCOUNT = json.loads(os.environ.get("GDRIVE_SERVICE_ACCOUNT", "{}"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DRIVE_FOLDER_ID = "1Hxw_9MI4qHGP8EHgiQ0nLkku_NNrY4fm"
MODEL_ID = "google/gemini-2.0-flash-lite-001"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TMP_DIR = "temp_clips"
os.makedirs(TMP_DIR, exist_ok=True)

# ── Helper: ask OpenRouter for scenes ───────────────────────────────────────────
def fetch_scenes(prompt):
    print(f"Fetching scene ideas with prompt: '{prompt}'")
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.7,
            }
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        scenes = []
        for line in text.splitlines():
            if "–" in line:
                movie, scene = line.split("–", 1)
                scenes.append((movie.strip(), scene.strip()))
        print(f"  \\_ Found {len(scenes)} scenes.")
        return scenes
    except Exception as e:
        print(f"❌ Error fetching scenes from AI: {e}")
        return []

# ── Build two lists: funny and classic ──────────────────────────────────────────
def get_target_scenes():
    funny_prompt = (
        "List the single funniest movie scene of all time released in 1990 or later. "
        "Respond only as: Movie Title – Brief, descriptive scene name."
    )
    classic_prompt = (
        "List the top two most iconic classic movie scenes of all time (any year). "
        "Respond each line only as: Movie Title – Brief, descriptive scene name."
    )
    funny = fetch_scenes(funny_prompt)
    classic = fetch_scenes(classic_prompt)
    return funny + classic

# ── Drive client init ───────────────────────────────────────────────────────────
def init_drive():
    creds = service_account.Credentials.from_service_account_info(
        GDRIVE_SERVICE_ACCOUNT,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

drive_service = init_drive()

# ── Check duplicate in Drive ────────────────────────────────────────────────────
def already_uploaded(name):
    q = f"name='{name}' and '{DRIVE_FOLDER_ID}' in parents and trashed=false"
    res = drive_service.files().list(q=q, fields="files(id)").execute()
    return bool(res.get("files"))

# ── Download from YouTube ───────────────────────────────────────────────────────
def download_clip(search_term):
    print(f"Downloading from YouTube with search: '{search_term}'")
    try:
        opts = {
            "format": "bestvideo[height<=1080]+bestaudio/best",
            "outtmpl": f"{TMP_DIR}/%(id)s.%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "default_search": "ytsearch1",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_term, download=True)
            entry = info["entries"][0] if "entries" in info else info
            filename = ydl.prepare_filename(entry)
            print(f"  \\_ Downloaded successfully: {filename}")
            return filename
    except Exception as e:
        print(f"❌ YouTube download failed: {e}")
        return None

# ── Reformat video to 1080×1920 with text overlay ──────────────────────────────
def transform_clip(in_path, out_path, movie_title):
    print(f"Transforming clip to vertical format with text overlay...")
    try:
        # Sanitize text for ffmpeg
        safe_title = movie_title.replace("'", "").replace(":", "")
        
        vf = (
            "scale=1080:-2,split=2[orig][bg];"
            "[bg]scale=1080:1920,boxblur=20[bgblur];"
            "[bgblur][orig]overlay=(W-w)/2:(H-h)/2,"
            f"drawtext=text='Movie: {safe_title}':"
            "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            "fontsize=48:fontcolor=white:x=(w-text_w)/2:y=150"
        )
        
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-vf", vf, "-c:a", "copy", out_path],
            check=True, capture_output=True
        )
        print(f"  \\_ Transformation complete: {out_path}")
        return out_path
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg transformation failed.")
        print(f"   \\_ STDERR: {e.stderr.decode()}")
        return None
    except Exception as e:
        print(f"❌ An unexpected error occurred during transformation: {e}")
        return None


# ── Upload to Google Drive ────────────────────────────────────────────────────
def upload_to_drive(local_path, name):
    print(f"Uploading '{name}' to Google Drive...")
    try:
        meta = {"name": name, "parents": [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(local_path, mimetype="video/mp4")
        drive_service.files().create(body=meta, media_body=media).execute()
        print(f"  \\_ Upload successful.")
    except Exception as e:
        print(f"❌ Google Drive upload failed: {e}")


# ── Main orchestration ─────────────────────────────────────────────────────────
def main():
    print("--- Starting Movie Clip Generation ---")
    scenes = get_target_scenes()
    if not scenes:
        print("Could not fetch scene ideas. Exiting.")
        return

    for movie, scene in scenes:
        final_filename = f"{movie} - {scene}.mp4".replace("/", "_")
        print(f"\\n→ Processing: {final_filename}")
        
        if already_uploaded(final_filename):
            print("  \\_ Already uploaded, skipping.")
            continue
            
        download_path = download_clip(f"{movie} {scene} scene")
        if not download_path:
            continue
            
        output_path = os.path.join(TMP_DIR, final_filename)
        
        transformed_path = transform_clip(download_path, output_path, movie)
        
        if transformed_path:
            upload_to_drive(transformed_path, final_filename)
            # Log to Google Sheets
            try:
                add_video_to_sheet(
                    source="Movie",
                    reddit_url=f"youtube.com (search: {movie} {scene})",
                    reddit_caption=f"{movie} - {scene}",
                    drive_video_name=final_filename.replace('.mp4','')
                )
                print("  \\_ Logged to Google Sheet.")
            except Exception as e:
                print(f"❌ Failed to log to Google Sheet: {e}")
        
        # Cleanup
        if os.path.exists(download_path):
            os.remove(download_path)
        if os.path.exists(output_path):
            os.remove(output_path)
    
    print("\\n--- Movie Clip Generation Complete ---")


if __name__ == "__main__":
    main()
