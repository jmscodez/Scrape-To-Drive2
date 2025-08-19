import os
import re
import subprocess
import json
import random
import praw
import yt_dlp
import cv2
import numpy as np
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------------ Google Drive Integration ------------------
SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_INFO = json.loads(os.environ['GDRIVE_SERVICE_ACCOUNT'])

def authenticate_drive():
    credentials = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=credentials)

def get_or_create_folder(drive_service, folder_name):
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    items = results.get('files', [])
    
    if items:
        return items[0]['id']
    else:
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

# ------------------ Video Processing ------------------
VIDEO_DOMAINS = {
    'reddit.com', 'v.redd.it', 'youtube.com', 'youtu.be',
    'streamable.com', 'gfycat.com', 'imgur.com', 'tiktok.com',
    'instagram.com', 'twitter.com', 'x.com', 'twitch.tv',
    'dailymotion.com', 'rumble.com'
}

def sanitize_filename(filename):
    """Sanitize the filename to avoid issues with long names and special characters"""
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename[:100]  # Limit filename length
    return filename.strip()

def get_video_info(video_path):
    """Get video dimensions and other properties using ffprobe"""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_streams', '-select_streams', 'v:0', video_path
        ], capture_output=True, text=True, check=True)
        
        import json
        data = json.loads(result.stdout)
        if data['streams']:
            stream = data['streams'][0]
            return {
                'width': int(stream.get('width', 0)),
                'height': int(stream.get('height', 0)),
                'duration': float(stream.get('duration', 0)),
                'codec': stream.get('codec_name', 'unknown')
            }
    except Exception as e:
        print(f"Error getting video info: {e}")
    return None

def download_video(url):
    try:
        ydl_opts = {
            'outtmpl': '%(id)s.%(ext)s',
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'cookiefile': 'cookies.txt',  # Add this line
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/122.0.0.0 Safari/537.36',
                'Referer': 'https://www.reddit.com/'
            },
            'extractor_args': {
                'reddit': {'skip_auth': True},
                'youtube': {'skip': ['dash', 'hls']},
                'twitter': {'include': ['native_video']}
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            
            # Get video info for debugging
            video_info = get_video_info(downloaded_file)
            if video_info:
                print(f"Video info: {video_info['width']}x{video_info['height']}, duration: {video_info['duration']}s")
            
            # Verify the file has audio
            result = subprocess.run(
                ['ffprobe', '-loglevel', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', downloaded_file],
                stdout=subprocess.PIPE,
                text=True
            )
            if 'audio' not in result.stdout:
                print("⚠️ Skipping: No audio track found")
                os.remove(downloaded_file)
                return None, 0
            return downloaded_file, info.get('duration', 0)
            
    except Exception as e:
        print(f"❌ Download failed for {url}: {str(e)}")
        return None, 0

def detect_tiktok_watermark(video_path):
    """
    Detect TikTok watermarks in video frames.
    Returns True if TikTok watermark is detected, False otherwise.
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("⚠️ Could not open video for watermark detection")
            return False
        
        # Get video properties
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        duration = total_frames / fps if fps > 0 else 0
        
        # Sample frames for detection (check every 2 seconds)
        sample_interval = max(1, int(fps * 2))
        frames_to_check = min(5, total_frames // sample_interval)  # Check up to 5 frames
        
        watermark_detected = False
        frames_checked = 0
        
        print(f"🔍 Checking {frames_to_check} frames for TikTok watermark...")
        
        for i in range(0, total_frames, sample_interval):
            if frames_checked >= frames_to_check:
                break
                
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            
            if not ret:
                continue
                
            frames_checked += 1
            
            # Check for TikTok watermark in this frame
            if _check_frame_for_tiktok_watermark(frame):
                watermark_detected = True
                print(f"🚫 TikTok watermark detected in frame {i}")
                break
        
        cap.release()
        
        if watermark_detected:
            print("❌ Skipping: TikTok watermark detected")
        else:
            print("✅ No TikTok watermark detected")
            
        return watermark_detected
        
    except Exception as e:
        print(f"⚠️ Error during watermark detection: {e}")
        return False

def _check_frame_for_tiktok_watermark(frame):
    """
    Check a single frame for TikTok watermark patterns.
    Returns True if watermark is detected, False otherwise.
    """
    try:
        height, width = frame.shape[:2]
        
        # Focus on bottom-left region where TikTok watermarks typically appear
        # Check bottom 25% and left 40% of the frame
        roi_height = int(height * 0.25)
        roi_width = int(width * 0.4)
        roi = frame[height - roi_height:height, 0:roi_width]
        
        if roi.size == 0:
            return False
        
        # Convert to different color spaces for better detection
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        
        # Method 1: Look for TikTok logo-like patterns (musical note icon)
        # TikTok logo typically has high contrast and specific shape
        _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Look for small, compact contours that could be the TikTok logo
        for contour in contours:
            area = cv2.contourArea(contour)
            if 50 < area < 2000:  # Reasonable size for TikTok logo
                x, y, w, h = cv2.boundingRect(contour)
                aspect_ratio = w / h if h > 0 else 0
                if 0.5 < aspect_ratio < 2.0:  # TikTok logo is roughly square-ish
                    # Check if it's in the expected position (near bottom-left)
                    if y > roi_height * 0.3:  # In bottom portion
                        return True
        
        # Method 2: Look for TikTok text patterns
        # TikTok watermarks often have white text on semi-transparent background
        # Look for regions with high brightness and specific text patterns
        
        # Create mask for bright regions (potential text)
        _, bright_mask = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
        
        # Look for horizontal lines of text (TikTok username format)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 1))
        horizontal_lines = cv2.morphologyEx(bright_mask, cv2.MORPH_OPEN, kernel)
        
        # Count horizontal text-like regions
        contours, _ = cv2.findContours(horizontal_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        text_like_regions = 0
        for contour in contours:
            area = cv2.contourArea(contour)
            if 100 < area < 5000:  # Reasonable size for text
                x, y, w, h = cv2.boundingRect(contour)
                aspect_ratio = w / h if h > 0 else 0
                if aspect_ratio > 3:  # Text is typically wide
                    text_like_regions += 1
        
        # If we find multiple text-like regions, it might be a TikTok watermark
        if text_like_regions >= 2:
            return True
        
        # Method 3: Look for characteristic TikTok watermark colors
        # TikTok watermarks often have specific color patterns
        
        # Look for white/light regions with semi-transparency
        white_mask = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 30, 255]))
        
        # Check if there are significant white regions in the expected area
        white_pixels = cv2.countNonZero(white_mask)
        total_pixels = roi.shape[0] * roi.shape[1]
        white_ratio = white_pixels / total_pixels if total_pixels > 0 else 0
        
        # If more than 15% of the ROI is white, it might be a watermark
        if white_ratio > 0.15:
            # Additional check: look for gradient patterns typical of TikTok watermarks
            # TikTok watermarks often have a gradient from transparent to opaque
            edges = cv2.Canny(gray, 50, 150)
            edge_density = cv2.countNonZero(edges) / total_pixels if total_pixels > 0 else 0
            
            # High edge density in bright regions suggests text/logo
            if edge_density > 0.05:
                return True
        
        return False
        
    except Exception as e:
        print(f"⚠️ Error in frame watermark detection: {e}")
        return False

def convert_to_tiktok(video_path):
    """Convert to 1080x1920 with a subtle crop offset and minor pitch shift."""
    try:
        output_path = video_path.replace(".mp4", "_VERTICAL.mp4")

        # Get video info to determine the best approach
        video_info = get_video_info(video_path)
        if video_info:
            print(f"Converting video: {video_info['width']}x{video_info['height']}")
            
            # If video is already close to 9:16 aspect ratio, use simple scaling
            aspect_ratio = video_info['width'] / video_info['height']
            if 0.5 <= aspect_ratio <= 0.6:  # Close to 9:16 (0.5625)
                print("Video is already close to 9:16, using simple scaling")
                vf = "scale=1080:1920:force_original_aspect_ratio=increase,setsar=1"
            else:
                # Use crop for other aspect ratios
                print("Using crop to achieve 9:16 aspect ratio")
                vf = (
                    "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920:(iw-1080)/2:(ih-1920)/2,"
                    "setsar=1"
                )
        else:
            # Fallback to simple scaling if we can't get video info
            print("Could not get video info, using simple scaling")
            vf = "scale=1080:1920:force_original_aspect_ratio=increase,setsar=1"

        # Very minor pitch shift without noticeable tempo change
        pitch_factor = random.choice([0.99, 1.01])
        atempo = 1.0 / pitch_factor
        af = f"asetrate=48000*{pitch_factor},aresample=48000,atempo={atempo:.6f}"

        # Add error handling and verbose output for debugging
        cmd = [
            'ffmpeg', '-y', '-i', video_path,
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-af', af,
            '-c:a', 'aac', '-b:a', '128k',
            output_path
        ]
        
        print(f"Running ffmpeg command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"FFmpeg stderr: {result.stderr}")
            print(f"FFmpeg stdout: {result.stdout}")
            
            # Try fallback approach without crop if the first attempt fails
            print("Trying fallback approach without crop...")
            vf_fallback = "scale=1080:1920:force_original_aspect_ratio=increase,setsar=1"
            
            cmd_fallback = [
                'ffmpeg', '-y', '-i', video_path,
                '-vf', vf_fallback,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-af', af,
                '-c:a', 'aac', '-b:a', '128k',
                output_path
            ]
            
            print(f"Running fallback ffmpeg command: {' '.join(cmd_fallback)}")
            
            result_fallback = subprocess.run(cmd_fallback, capture_output=True, text=True)
            
            if result_fallback.returncode != 0:
                print(f"Fallback FFmpeg stderr: {result_fallback.stderr}")
                print(f"Fallback FFmpeg stdout: {result_fallback.stdout}")
                raise subprocess.CalledProcessError(result_fallback.returncode, cmd_fallback, result_fallback.stdout, result_fallback.stderr)
            
        return output_path
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        return None

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0 (by /u/Proof_Difficulty_396)"
)

if __name__ == "__main__":
    processed = 0
    target = 5  # Changed from 3 to 5 for dog videos
    
    # Configuration options
    ENABLE_WATERMARK_DETECTION = True  # Set to False to disable TikTok watermark detection
    
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "Dog Videos")  # Changed folder name

    print("\n" + "="*40)
    print(f"🚀 Processing {target} videos from r/dogvideos")
    print(f"🔍 TikTok watermark detection: {'ENABLED' if ENABLE_WATERMARK_DETECTION else 'DISABLED'}")
    print("="*40)

    for post in reddit.subreddit("dogvideos").top(time_filter="day", limit=50):  # Changed subreddit
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            
            # Skip if not a video domain
            if not any(domain in post.url for domain in VIDEO_DOMAINS):
                print(f"⚠️ Skipping: Unsupported URL - {post.url}")
                continue
                
            video_path, duration = download_video(post.url)
            if not video_path:
                continue
                
            # Check if file exists and is valid
            if not os.path.exists(video_path):
                print(f"⚠️ Skipping: Downloaded file not found - {video_path}")
                continue
                
            if not (10 <= duration <= 180):
                print(f"⚠️ Skipping: Duration {duration}s out of range")
                os.remove(video_path)
                continue
            
            # Check for TikTok watermark before processing
            if ENABLE_WATERMARK_DETECTION and detect_tiktok_watermark(video_path):
                print(f"🚫 Skipping: TikTok watermark detected in {post.title[:50]}...")
                os.remove(video_path)
                continue
            
            vertical_path = convert_to_tiktok(video_path)
            if video_path and os.path.exists(video_path):
                os.remove(video_path)  # Clean up original video
            
            if vertical_path:
                sanitized_title = sanitize_filename(post.title)
                final_path = f"{sanitized_title}.mp4"
                os.rename(vertical_path, final_path)
                
                upload_to_drive(drive_service, folder_id, final_path)
                os.remove(final_path)
                processed += 1
                print(f"✅ Success: {sanitized_title}")

        except Exception as e:
            print(f"⚠️ Error processing post {post.id}: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} videos processed")
    print("="*40)
