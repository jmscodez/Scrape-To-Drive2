import os
import re
import subprocess
import json
import random
import praw
import yt_dlp
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
    
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "Dog Videos")  # Changed folder name

    print("\n" + "="*40)
    print(f"🚀 Processing {target} videos from r/dogvideos")
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
