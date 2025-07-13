import os
import re
import subprocess
import requests
from yt_dlp import YoutubeDL
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont

# ── Configuration ──────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DRIVE_FOLDER_ID    = "1Hxw_9MI4qHGP8EHgiQ0nLkku_NNrY4fm"
MODEL_ID           = "google/gemini-2.0-flash-lite-001"
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

TMP_DIR = "temp_clips"
os.makedirs(TMP_DIR, exist_ok=True)

# ── Helper: ask OpenRouter for scenes ───────────────────────────────────────────
def fetch_scenes(prompt):
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
            # Split from the right to robustly separate movie from scene
            movie, scene = line.rsplit("–", 1)
            # Clean up markdown, list numbers, and other search-breaking characters
            clean_movie = re.sub(r"^\s*\d+\.\s*", "", movie).strip().replace("*", "").replace("_", "").replace(":", "")
            clean_scene = scene.strip().replace("*", "").replace("_", "").replace("[", "").replace("]", "")
            scenes.append((clean_movie, clean_scene))
    return scenes

# ── Generate a creative title with OpenRouter ──────────────────────────────────
def generate_creative_title(movie, scene):
    prompt = (
        f"You are a creative assistant for a social media account that posts movie clips. "
        f"Your task is to generate a short, viral-style title for a specific movie scene. "
        f"The title should be in a 'When...' or 'POV:' format, be under 12 words, and include ONE relevant emoji from this list: 😂, 🔥, 🤯, 😭, 🍿, 🎬, ❤️.\n\n"
        f"Movie: {movie}\n"
        f"Scene: {scene}\n\n"
        f"Respond with ONLY the creative title."
    )
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "mistralai/mistral-7b-instruct", # Using a different model to avoid strict safety filters
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0.7,
            }
        )
        resp.raise_for_status()
        title = resp.json()["choices"][0]["message"]["content"].strip()
        # Final cleanup to remove any accidental quotes
        return title.replace('"', '').replace("'", "")
    except requests.exceptions.HTTPError as e:
        print(f"   ⚠️ Could not generate creative title (API Error: {e}). Falling back to movie title.")
        return movie

# ── Create dynamic title bubble ────────────────────────────────────────────────
def create_dynamic_bubble(text, font_path, font_size, output_path):
    # 1. Load font and measure text dimensions
    font = ImageFont.truetype(font_path, font_size)
    text_bbox = font.getbbox(text)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    # 2. Define bubble properties with padding
    padding = 30
    bubble_width = text_width + padding * 2
    bubble_height = text_height + padding * 2
    corner_radius = 25
    notch_height = 20
    notch_width = 40

    # 3. Create a new transparent image sized for the bubble and notch
    image = Image.new("RGBA", (int(bubble_width), int(bubble_height + notch_height)), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    # 4. Draw the rounded rectangle and the notch to form the bubble
    draw.rounded_rectangle((0, 0, bubble_width, bubble_height), fill="white", radius=corner_radius)
    notch_start_x = bubble_width - corner_radius - notch_width
    notch_points = [
        (notch_start_x, bubble_height - 1), # -1 to slightly overlap and avoid rendering gaps
        (notch_start_x + notch_width, bubble_height - 1),
        (notch_start_x + notch_width / 2, bubble_height + notch_height)
    ]
    draw.polygon(notch_points, fill="white")

    # 5. Draw the title text onto the bubble
    text_x = padding
    text_y = padding - text_bbox[1] # Adjust for font's internal top padding
    draw.text((text_x, text_y), text, font=font, fill="black")

    # 6. Save the final bubble image
    image.save(output_path)
    return output_path

# ── Build two lists: funny and classic ──────────────────────────────────────────
def get_target_scenes():
    funny_prompt = (
        "List the single funniest movie scene of all time released in 1990 or later. "
        "Respond only as: Movie Title – Brief, descriptive scene name (under 10 words)."
    )
    classic_prompt = (
        "List the top two most iconic classic movie scenes of all time (any year). "
        "Respond each on a new line, only as: Movie Title – Brief, descriptive scene name (under 10 words). "
        "Do not use list numbers."
    )
    funny = fetch_scenes(funny_prompt)      # returns [(movie,scene)]
    classic = fetch_scenes(classic_prompt)  # returns [(movie,scene), …]
    return funny + classic                  # total of 3 items

# ── Drive client init ───────────────────────────────────────────────────────────
def init_drive():
    creds = Credentials.from_service_account_file(
        "service_account.json",
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

drive_service = init_drive()

# ── Check duplicate in Drive ────────────────────────────────────────────────────
def already_uploaded(name):
    # Escape single quotes in the filename for the Drive API query.
    escaped_name = name.replace("'", "\\'")
    q = f"name='{escaped_name}' and '{DRIVE_FOLDER_ID}' in parents and trashed=false"
    res = drive_service.files().list(q=q, fields="files(id)").execute()
    return bool(res.get("files"))

# ── Download from YouTube ───────────────────────────────────────────────────────
def download_clip(search_term):
    opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": f"{TMP_DIR}/%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "default_search": "ytsearch1",
        "cookiefile": "YT_Cookies.txt",
    }
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(search_term, download=True)
            # Handle cases where search yields no results
            if info and info.get("entries"):
                entry = info["entries"][0]
                return ydl.prepare_filename(entry)
            print("   → No search results found on YouTube.")
            return None
        except Exception as e:
            # Catch other download errors, e.g., video unavailable
            print(f"❌ YouTube download failed: {e}")
            return None

# ── Reformat video to 1080×1920 with blurred bars and title ─────────────────────
def transform_clip(in_p, out_p, bubble_path):
    # Define the audio normalization filter for professional-sounding audio.
    af_normalize = "loudnorm=I=-16:TP=-1.5:LRA=11"

    # Base video layers (blur, scale, overlay)
    vf_base = (
        "[0:v]split[original][background];"
        "[background]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20[blurred_background];"
        "[original]scale=1080:-2[foreground];"
        "[blurred_background][foreground]overlay=(W-w)/2:(H-h)/2[base_video]"
    )
    
    # Complex filter to overlay the dynamically generated bubble image.
    vf_complex = (
        f"{vf_base};"
        f"[1:v]scale=w=1080*0.9:-1[bubble];" # Scale bubble to 90% of video width
        f"[base_video][bubble]overlay=(W-w)/2:120" # Position bubble near the top
    )

    command = [
        "ffmpeg", "-y", "-i", in_p, "-i", bubble_path,
        "-filter_complex", vf_complex,
        "-af", af_normalize, "-c:a", "aac", # Apply audio normalization
        out_p
    ]
    
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

# ── Upload to Google Drive ────────────────────────────────────────────────────
def upload_to_drive(local_path, name):
    if already_uploaded(name):
        print(f"Skipped (exists): {name}")
        return
    meta  = {"name": name, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(local_path, mimetype="video/mp4")
    drive_service.files().create(body=meta, media_body=media).execute()
    print(f"Uploaded: {name}")

# ── Main orchestration ─────────────────────────────────────────────────────────
def main():
    scenes = get_target_scenes()  # 1 funny + 2 classic
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    for movie, scene in scenes:
        print(f"→ Processing scene from '{movie}': {scene}")

        creative_title = generate_creative_title(movie, scene)
        print(f"   → Creative title: '{creative_title}'")
        
        # Sanitize the creative title to be a valid filename
        safe_fname = re.sub(r'[\\/*?:"<>|]', "", creative_title) + ".mp4"
        
        if already_uploaded(safe_fname):
            print(f"   → Already uploaded as '{safe_fname}', skipping.")
            continue
        
        # Create the dynamic title bubble image
        temp_bubble_path = os.path.join(TMP_DIR, f"{safe_fname}_bubble.png")
        try:
            create_dynamic_bubble(creative_title, font_path, 55, temp_bubble_path)
            print(f"   → Generated dynamic title card.")
        except Exception as e:
            print(f"   ❌ Failed to generate dynamic title card: {e}")
            continue # Skip this clip if bubble generation fails

        search_query = f"{movie} {scene} scene"
        print(f"   → Downloading from YouTube with search: '{search_query}'")
        downloaded_clip_path = download_clip(search_query)

        if not downloaded_clip_path:
            os.remove(temp_bubble_path) # Clean up if download fails
            continue

        output_video_path = os.path.join(TMP_DIR, safe_fname)
        
        try:
            transform_clip(downloaded_clip_path, output_video_path, temp_bubble_path)
            upload_to_drive(output_video_path, safe_fname)
            print(f"   → Successfully processed and uploaded '{safe_fname}'")
        finally:
            # Ensure all temporary files are cleaned up
            if os.path.exists(downloaded_clip_path):
                os.remove(downloaded_clip_path)
            if os.path.exists(temp_bubble_path):
                os.remove(temp_bubble_path)
            if os.path.exists(output_video_path):
                os.remove(output_video_path)


if __name__ == "__main__":
    main()
