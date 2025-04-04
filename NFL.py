import praw
import yt_dlp
import os
import subprocess
import re
import google.generativeai as genai
import textwrap
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive Setup
SERVICE_ACCOUNT_FILE = 'service_account.json'
SCOPES = ['https://www.googleapis.com/auth/drive']
PARENT_FOLDER_ID = '1IjWmMJJKp3BMhVINrSbwkL1HIT5277WF'  # Replace with your Google Drive folder ID

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

# Reddit API Setup
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT')
)

# Google Gemini API Setup
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

def upload_to_drive(file_path, folder_id=PARENT_FOLDER_ID):
    try:
        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [folder_id]
        }
        media = MediaFileUpload(file_path, mimetype='video/mp4')
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        print(f"✅ Uploaded to Google Drive: {file_path}")
        return file.get('id')
    except Exception as e:
        print(f"❌ Google Drive upload failed: {str(e)}")
        return None

def download_video(url):
    try:
        folder = "videos"
        os.makedirs(folder, exist_ok=True)
        
        ydl_opts = {
            'outtmpl': f'{folder}/%(title)s.%(ext)s',
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'referer': 'https://www.reddit.com/',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
            },
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            
            result = subprocess.run(
                ['ffprobe', '-loglevel', 'error', '-select_streams', 'a', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', downloaded_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            if 'audio' not in result.stdout:
                print("⚠️ Skipping: No audio track found")
                os.remove(downloaded_file)
                return None, 0
                
            return downloaded_file, info.get('duration', 0)
            
    except Exception as e:
        print(f"❌ Download failed: {str(e)}")
        return None, 0

def convert_to_tiktok(video_path):
    try:
        output_path = video_path.replace(".mp4", "_VERTICAL.mp4")
        cmd = [
            'ffmpeg', '-i', video_path,
            '-vf', 
            'split [original][blur];'
            '[blur] scale=1080:1920, gblur=sigma=20, setsar=1 [bg];'
            '[original] scale=1080:1080:force_original_aspect_ratio=increase,'
            'crop=1080:1080:exact=1 [scaled];'
            '[bg][scaled] overlay=(W-w)/2:(H-h)/2:format=auto,'
            'setdar=9/16,setsar=1',
            '-c:a', 'copy',
            '-y', output_path
        ]
        subprocess.run(cmd, check=True)
        return output_path
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        return None

def generate_short_caption(text):
    try:
        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        prompt = f"""Transform this Reddit title into a viral social media caption:
        1. Remove ALL brackets, if applicble, you should try and use the name brackets as a quote (if it's a name)
        2. Identify and extract key quotes with attribution.
        3. Make it 8-12 words max.
        4. Use attention-grabbing phrasing.
        5. Add context if needed.
        6. Never use markdown.

        Example transformations:
        - "[NFL Films] His name is Baun, Zack Baun..." → "Zack Baun's iconic play: Same move, same result 1 month apart"
        - "[Coach] 'We need better defense'" → "Coach demands: Better defense crucial for championship"
        - "Fan says 'This was the best game ever!'" → "Fan reacts: Best game ever witnessed"

        Given this title: {text}
        """

        model = genai.GenerativeModel("gemini-pro")
        response = model.generate_content(prompt)
        
        return response.text.strip() if response and response.text else " ".join(text.split()[:12])

    except Exception as e:
        print(f"⚠️ AI caption failed: {str(e)}")
        return " ".join(text.split()[:12])

def add_caption(video_path, text):
    try:
        original_text = text
        
        if len(text.split()) > 14:
            text = generate_short_caption(text)
        
        wrapped_text = textwrap.fill(text, width=28,
                                   break_long_words=True,
                                   break_on_hyphens=False)
        
        cmd = [
            'ffmpeg', '-y', '-i', video_path,
            '-vf', f"drawtext=text='{wrapped_text}':"
                   "fontfile=/Library/Fonts/ProximaNova-ExtraBold.ttf:"
                   "fontsize=58:"
                   "fontcolor=white:"
                   "bordercolor=black:"
                   "borderw=5:"
                   "x=(w-tw)/2:"
                   "y=h/12:"
                   "text_align=center",
            '-c:a', 'copy',
            video_path.replace(".mp4", "_FINAL.mp4")
        ]
        
        subprocess.run(cmd, check=True)
        return video_path.replace(".mp4", "_FINAL.mp4")
        
    except Exception as e:
        print(f"❌ Caption failed: {str(e)} - Original text: {original_text[:50]}...")
        return None
    
if __name__ == "__main__":
    processed = 0
    target = 5
    
    print("\n" + "="*40)
    print(f"🚀 Processing 5 videos from r/NFL")
    print("="*40)

    posts = reddit.subreddit("NFL").top(time_filter="day", limit=50)
    
    for post in posts:
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            
            if not (hasattr(post, 'is_video') and post.is_video) or hasattr(post, 'crosspost_parent'):
                print("⚠️ Skipping: Not a native Reddit video")
                continue
                
            if not hasattr(post, 'media') or not post.media or 'reddit_video' not in post.media:
                print("⚠️ Skipping: Invalid video metadata")
                continue
                
            if not post.media['reddit_video'].get('has_audio', False):
                print("⚠️ Skipping: No audio track")
                continue
                
            video_path, duration = download_video(post.url)
            if not video_path:
                continue
                
            if not (10 <= duration <= 180):
                print(f"⚠️ Skipping: Duration {duration}s out of range")
                os.remove(video_path)
                continue
            
            vertical_path = convert_to_tiktok(video_path)
            os.remove(video_path)
            final_path = add_caption(vertical_path, post.title)
            os.remove(vertical_path)
            
            # Upload to Google Drive
            drive_id = upload_to_drive(final_path)
            if drive_id:
                processed += 1
                print(f"✅ Success: {os.path.basename(final_path)}")
                os.remove(final_path)
            
        except Exception as e:
            print(f"⚠️ Error processing: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/5 videos processed")
    print("="*40)
