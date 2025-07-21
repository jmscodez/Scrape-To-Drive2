#!/usr/bin/env python3

import os
import sys
import subprocess
import yt_dlp
import requests
from pathlib import Path

# Import existing functions - avoiding NBA.py to prevent dependency issues
import json
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive functions (copied from NBA.py to avoid import issues)
SCOPES = ['https://www.googleapis.com/auth/drive']

def authenticate_drive():
    SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
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

def convert_to_tiktok(video_path):
    """Convert video to 9:16 vertical format (copied from NBA.py)"""
    width, height = get_video_resolution(video_path)
    if not width or not height:
        print("⚠️ Could not get resolution; defaulting to simple crop")
        method = 'simple'
    else:
        aspect = width/height
        method = 'simple' if abs(aspect - 9/16) < 0.02 else 'blur'

    output = video_path.replace(".mp4","_VERTICAL.mp4")

    if method == 'simple':
        cmd = [
            'ffmpeg','-i',video_path,
            '-vf','scale=1080:1920:force_original_aspect_ratio=increase,'
                 'crop=1080:1920,setsar=1',
            '-c:v','libx264','-preset','fast','-crf','23',
            '-c:a','aac','-y', output
        ]
    else:
        sq = min(width, height)
        x_off = (width - sq)/2
        y_off = (height - sq)/2
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

def download_youtube_video(url):
    """Download YouTube video and return file path, title, and duration"""
    Path("temp").mkdir(exist_ok=True)
    
    ydl_opts = {
        'outtmpl': 'temp/%(title)s.%(ext)s',
        'format': 'bestvideo[height<=1080]+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            title = info.get('title', 'Unknown Video')
            duration = info.get('duration', 0)
            print(f"✅ Downloaded: {title} ({duration}s)")
            return filename, title, duration
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return None, None, 0

def get_video_duration(video_path):
    """Get video duration using ffprobe"""
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
               '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except:
        return 0

def calculate_clips(duration, num_clips):
    """Calculate clip segments"""
    if duration <= 0 or num_clips <= 0:
        return []
    
    clip_length = duration / num_clips
    clips = []
    
    for i in range(num_clips):
        start = i * clip_length
        end = min(start + clip_length, duration)
        clips.append((start, end))
    
    return clips

def generate_tiktok_title(original_title):
    """Generate TikTok title using OpenRouter API"""
    try:
        prompt = f"Create a short, catchy TikTok title (max 50 chars, 1-2 emojis, no hashtags) from: '{original_title}'"
        
        headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"}
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [
                {"role": "system", "content": "You are a viral TikTok content expert."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 60,
            "temperature": 0.8
        }
        
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        
        title = response.json()['choices'][0]['message']['content'].strip()
        title = title.replace('"', '').replace("'", '').replace('#', '')[:50]
        return title
        
    except Exception as e:
        print(f"⚠️ Title generation failed: {e}")
        return original_title[:50]

def create_clips(video_path, clips_info):
    """Create individual clip files"""
    Path("temp/clips").mkdir(parents=True, exist_ok=True)
    clip_files = []
    
    for i, (start_time, end_time) in enumerate(clips_info, 1):
        duration = end_time - start_time
        output_file = f"temp/clips/clip_{i:03d}.mp4"
        
        try:
            cmd = ['ffmpeg', '-i', video_path, '-ss', str(start_time), '-t', str(duration), 
                   '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', output_file]
            subprocess.run(cmd, check=True, capture_output=True)
            clip_files.append(output_file)
            print(f"✅ Created clip {i}: {duration:.1f}s")
        except Exception as e:
            print(f"❌ Failed to create clip {i}: {e}")
    
    return clip_files

def main(youtube_url, num_clips, drive_folder_name):
    """Main processing function"""
    print(f"🎬 Processing {youtube_url} into {num_clips} clips for folder '{drive_folder_name}'")
    
    # Clean temp directory
    import shutil
    if Path("temp").exists():
        shutil.rmtree("temp")
    
    try:
        # Download video
        print("🔽 Downloading video...")
        video_path, title, duration = download_youtube_video(youtube_url)
        if not video_path:
            return False
        
        # Get precise duration
        precise_duration = get_video_duration(video_path)
        if precise_duration > 0:
            duration = precise_duration
        
        print(f"📊 Video duration: {duration:.1f}s ({duration/60:.1f} minutes)")
        
        # Convert to vertical
        print("🔄 Converting to vertical format...")
        vertical_video = convert_to_tiktok(video_path)
        if not vertical_video:
            print("❌ Conversion failed")
            return False
        
        # Calculate clips
        clips_info = calculate_clips(duration, num_clips)
        print(f"📐 Will create {len(clips_info)} clips")
        
        # Create clips
        print("✂️ Creating clips...")
        clip_files = create_clips(vertical_video, clips_info)
        
        # Generate title
        print("🎨 Generating TikTok title...")
        tiktok_title = generate_tiktok_title(title)
        print(f"📝 Title: {tiktok_title}")
        
        # Upload to Drive
        print("☁️ Uploading to Google Drive...")
        drive_service = authenticate_drive()
        folder_id = get_or_create_folder(drive_service, drive_folder_name)
        
        uploaded = 0
        for i, clip_file in enumerate(clip_files, 1):
            safe_title = sanitize_filename(tiktok_title)
            final_name = f"{i}_{safe_title}.mp4"
            final_path = f"temp/clips/{final_name}"
            os.rename(clip_file, final_path)
            
            try:
                upload_to_drive(drive_service, folder_id, final_path)
                uploaded += 1
                print(f"✅ Uploaded: {final_name}")
            except Exception as e:
                print(f"❌ Upload failed: {e}")
        
        print(f"🎉 Success! Uploaded {uploaded}/{len(clip_files)} clips")
        return True
        
    except Exception as e:
        print(f"❌ Process failed: {e}")
        return False
        
    finally:
        if Path("temp").exists():
            shutil.rmtree("temp")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python clip_video_simple.py <youtube_url> <num_clips> <drive_folder>")
        sys.exit(1)
    
    youtube_url = sys.argv[1]
    num_clips = int(sys.argv[2])
    drive_folder = sys.argv[3]
    
    success = main(youtube_url, num_clips, drive_folder)
    sys.exit(0 if success else 1) # Force refresh
