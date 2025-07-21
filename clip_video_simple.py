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

def get_or_create_folder(drive_service, folder_name):
    """Get or create Google Drive folder"""
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
    """Upload file to Google Drive folder"""
    name = os.path.basename(file_path)
    media = MediaFileUpload(file_path)
    drive_service.files().create(
        body={'name': name, 'parents': [folder_id]},
        media_body=media
    ).execute()
    print(f"✅ Uploaded {name} to Google Drive")

def generate_tiktok_title(original_title):
    """Generate TikTok-optimized title using OpenRouter API"""
    try:
        prompt = (
            "Create a short, catchy TikTok title from this YouTube video title. "
            "Rules: Max 50 characters, use 1-2 relevant emojis, make it engaging for TikTok/Reels, "
            "focus on the most interesting part, NO HASHTAGS. "
            f"Original title: '{original_title}'"
        )
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [
                {"role": "system", "content": "You are a viral TikTok content expert who creates engaging short titles."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 60,
            "temperature": 0.8
        }
        
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers=headers
        )
        response.raise_for_status()
        
        title = response.json()['choices'][0]['message']['content'].strip()
        # Clean up title
        title = re.sub(r'["\']', '', title)  # Remove quotes
        title = re.sub(r'#\w+', '', title)     # Remove hashtags
        title = title.strip()[:50]             # Limit length
        
        return title
        
    except Exception as e:
        print(f"⚠️ Title generation failed: {e}")
        # Fallback: clean up original title
        clean_title = re.sub(r'[^\w\s-]', '', original_title)
        return clean_title[:50].strip()

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
    Convert video to 9:16 vertical format
    - Blurs & scales the center square to 1080×1920  
    - Overlays the sharp 1080×1080 center crop
    """
    filter_complex = "[0:v]crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,split=2[fg_crop][bg_crop];[bg_crop]scale=1080:1920,setsar=1,gblur=sigma=20[bg];[fg_crop]scale=1080:1080,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[vid]"

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
        
        # Step 4: Generate TikTok title
        print("\n🎨 Generating TikTok title...")
        tiktok_title = generate_tiktok_title(title)
        print(f"📝 Generated title: {tiktok_title}")
        
        # Step 5: Upload to Google Drive
        print(f"\n☁️ Uploading {len(clip_files)} clips to Google Drive...")
        drive_service = authenticate_drive()
        folder_id = get_or_create_folder(drive_service, drive_folder_name)
        
        uploaded_count = 0
        for i, clip_file in enumerate(clip_files, 1):
            # Create filename: 1_TikTok Title, 2_TikTok Title, etc.
            safe_title = sanitize_filename(tiktok_title)
            final_name = f"{i}_{safe_title}.mp4"
            final_path = work_dir / "clips" / final_name
            
            # Rename file before upload
            clip_file.rename(final_path)
            
            try:
                upload_to_drive(drive_service, folder_id, final_path)
                uploaded_count += 1
                print(f"📤 Uploaded: {final_name}")
            except Exception as e:
                print(f"❌ Upload failed for clip {i}: {e}")
        
        print(f"\n🎉 Success! Uploaded {uploaded_count}/{len(clip_files)} clips to '{drive_folder_name}' folder")
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
