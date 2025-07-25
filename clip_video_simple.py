#!/usr/bin/env python3

import os
import sys
import subprocess
import json
import re
import requests
import shutil
from pathlib import Path

import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── CONFIGURATION ───────────────────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/drive']
PARENT_DRIVE_FOLDER_ID = '1XduvuA7AyiuxvY9SdL5eGwBDDVbbdECa'

# ── HELPER FUNCTIONS ─────────────────────────────────────────────────────────
def sanitize_filename(fn):
    """Clean filename for safe filesystem use"""
    fn = re.sub(r'[\\/*?:"<>|]', "", fn)
    return fn.strip()[:100]

def authenticate_drive():
    """Authenticate with Google Drive using service account"""
    SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])
    creds = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)

def get_or_create_subfolder(drive_service, parent_folder_id, subfolder_name):
    """Get or create a subfolder within a specific parent folder."""
    # Check if folder already exists
    q = f"name='{subfolder_name}' and '{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive_service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    items = res.get('files', [])
    
    if items:
        folder_id = items[0]['id']
        print(f"📁 Found existing subfolder '{subfolder_name}' (ID: {folder_id})")
        return folder_id
    
    # If not, create it
    print(f"📁 Creating new subfolder '{subfolder_name}'...")
    folder_metadata = {
        'name': subfolder_name,
        'parents': [parent_folder_id],
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = drive_service.files().create(
        body=folder_metadata,
        fields='id'
    ).execute()
    folder_id = folder.get('id')
    print(f"✅ Created subfolder with ID: {folder_id}")
    return folder_id

def upload_to_drive(drive_service, folder_id, file_path):
    """Upload file to Google Drive folder"""
    name = os.path.basename(file_path)
    media = MediaFileUpload(file_path)
    drive_service.files().create(
        body={'name': name, 'parents': [folder_id]},
        media_body=media
    ).execute()
    print(f"✅ Uploaded {name} to Google Drive")

# This function is no longer needed
# def generate_tiktok_title(original_title): ...

def download_youtube_video(url, work_dir, cookie_file=None):
    """Download YouTube video with robust error handling"""
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / "input.mp4"
    
    # Basic download options
    opts = {
        'format': 'bestvideo[height>=480]+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': str(dest),
        'quiet': True,
    }
    
    # Use cookies if provided
    if cookie_file and Path(cookie_file).exists():
        print(f"🍪 Using cookies from {cookie_file}")
        opts['cookiefile'] = cookie_file
    elif cookie_file:
        print(f"⚠️ Cookie file not found at '{cookie_file}'. Proceeding without cookies.")
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Get video info first
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Unknown Video')
            duration = info.get('duration', 0)
            
            # Check minimum duration (at least 60 seconds)
            if duration < 60:
                raise RuntimeError(f"Video too short: {duration}s (need at least 60s)")
            
            print(f"✅ Found video: {title} ({duration}s)")
            
            # Download the video
            ydl.extract_info(url, download=True)
            
            # Verify resolution
            result = subprocess.check_output([
                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=s=x:p=0', str(dest)
            ]).decode().strip()
            
            w, h = map(int, result.split('x'))
            if min(w, h) < 480:
                raise RuntimeError(f"Resolution too low: {w}x{h} (need at least 480p)")
            
            print(f"✅ Downloaded successfully: {w}x{h}")
            return dest, title, duration
            
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return None, None, 0

def reformat_to_916(src_path, dst_path):
    """
    Convert video to 9:16 vertical format while preserving the original 16:9 aspect ratio.
    - The full 16:9 video is scaled to fit the width of the 1080x1920 frame.
    - The background is a blurred, stretched version of the source video.
    """
    filter_complex = (
        "[0:v]split=2[bg_src][fg_src];"
        "[bg_src]scale=1080:1920,setsar=1,gblur=sigma=20[bg];"
        "[fg_src]scale=1080:-1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[vid]"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-filter_complex", filter_complex,
        "-map", "[vid]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p", 
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst_path)
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"✅ Converted to 9:16 format")
        return dst_path
    except subprocess.CalledProcessError as e:
        print(f"❌ Format conversion failed: {e}")
        return None

def create_clips(video_path, duration, num_clips, work_dir):
    """Create evenly spaced clips from the video"""
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    
    clip_duration = duration / num_clips
    min_clip_length = max(30, clip_duration * 0.8)  # At least 30s or 80% of target
    
    clip_files = []
    current_start = 0
    
    print(f"📐 Creating {num_clips} clips from {duration:.1f}s video")
    
    for i in range(num_clips):
        if i == num_clips - 1:  # Last clip gets remainder
            clip_end = duration
        else:
            clip_end = current_start + max(min_clip_length, clip_duration)
            if clip_end > duration:
                clip_end = duration
        
        actual_duration = clip_end - current_start
        output_file = clips_dir / f"clip_{i+1:03d}.mp4"
        
        try:
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(current_start),
                '-t', str(actual_duration),
                '-i', str(video_path),
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                str(output_file)
            ]
            
            subprocess.run(cmd, check=True, capture_output=True)
            clip_files.append(output_file)
            print(f"✅ Created clip {i+1}: {actual_duration:.1f}s ({current_start:.1f}s - {clip_end:.1f}s)")
            
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to create clip {i+1}: {e}")
            continue
            
        current_start = clip_end
        
        # Stop if we've reached the end
        if current_start >= duration:
            break
    
    return clip_files

def main(youtube_url, num_clips, drive_folder_name, cookie_file=None):
    """Main processing function"""
    print(f"🎬 Starting YouTube video clipper...")
    print(f"📹 URL: {youtube_url}")
    print(f"✂️ Clips: {num_clips}")
    print(f"📁 Drive folder: {drive_folder_name}")
    
    # Setup workspace
    work_dir = Path("temp")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Step 1: Download video
        print("\n🔽 Downloading video...")
        video_path, title, duration = download_youtube_video(youtube_url, work_dir, cookie_file)
        if not video_path:
            return False
        
        print(f"📊 Video duration: {duration:.1f}s ({duration/60:.1f} minutes)")
        
        # Step 2: Convert to vertical format
        print("\n🔄 Converting to 9:16 vertical format...")
        vertical_path = work_dir / "vertical.mp4"
        converted_video = reformat_to_916(video_path, vertical_path)
        if not converted_video:
            print("❌ Failed to convert to vertical format")
            return False
        
        # Step 3: Create clips
        print(f"\n✂️ Creating {num_clips} clips...")
        clip_files = create_clips(converted_video, duration, num_clips, work_dir)
        
        if not clip_files:
            print("❌ No clips were created")
            return False
        
        # Step 4: No longer generating AI title, we will use the original title.
        print("\n📝 Using original YouTube video title for filenames.")
        
        # Step 5: Upload to Google Drive
        print(f"\n☁️ Uploading {len(clip_files)} clips to Google Drive...")
        drive_service = authenticate_drive()
        # Create a subfolder within the main "Custom Clips" folder
        subfolder_id = get_or_create_subfolder(drive_service, PARENT_DRIVE_FOLDER_ID, drive_folder_name)
        
        uploaded_count = 0
        for i, clip_file in enumerate(clip_files, 1):
            # Use original YouTube title for the filename
            safe_title = sanitize_filename(title)
            final_name = f"{i}_{safe_title}.mp4"
            final_path = work_dir / "clips" / final_name
            
            # Rename file before upload
            clip_file.rename(final_path)
            
            try:
                # Upload to the newly created subfolder
                upload_to_drive(drive_service, subfolder_id, final_path)
                uploaded_count += 1
                print(f"📤 Uploaded: {final_name}")
            except Exception as e:
                print(f"❌ Upload failed for clip {i}: {e}")
        
        print(f"\n🎉 Success! Uploaded {uploaded_count}/{len(clip_files)} clips to subfolder '{drive_folder_name}'")
        return True
        
    except Exception as e:
        print(f"❌ Process failed: {e}")
        return False
        
    finally:
        # Cleanup
        print("\n🧹 Cleaning up temporary files...")
        if work_dir.exists():
            shutil.rmtree(work_dir)

if __name__ == "__main__":
    if len(sys.argv) not in [4, 5]:
        print("Usage: python clip_video_simple.py <youtube_url> <num_clips> <drive_folder> [cookie_file]")
        print("\nExample:")
        print("python clip_video_simple.py 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' 4 'My Clips' 'cookies.txt'")
        sys.exit(1)
    
    youtube_url = sys.argv[1]
    num_clips = int(sys.argv[2])
    drive_folder = sys.argv[3]
    cookie_file = sys.argv[4] if len(sys.argv) == 5 else None

    # Validate inputs
    if num_clips < 1 or num_clips > 20:
        print("❌ Number of clips must be between 1 and 20")
        sys.exit(1)
    
    if not youtube_url.startswith(('https://www.youtube.com/', 'https://youtu.be/')):
        print("❌ Please provide a valid YouTube URL")
        sys.exit(1)
    
    success = main(youtube_url, num_clips, drive_folder, cookie_file)
    sys.exit(0 if success else 1)
