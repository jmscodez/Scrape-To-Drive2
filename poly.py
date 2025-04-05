import os
import re
import subprocess
import time
import textwrap
import json
from datetime import datetime, timedelta

import praw
import yt_dlp
import google.generativeai as genai

# ------------------ Google Drive Integration ------------------
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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

# ------------------ Reddit API Setup ------------------
reddit = praw.Reddit(
    client_id="ukxK5Yas7IaNzUo38ctyTA",
    client_secret="p2XZicA9MtqK4AJwgtJ4HO-DL4Ud-w",
    user_agent="script:mybot:v1.0 (by /u/Proof_Difficulty_396)"
)

# ------------------ OpenAI API Setup ------------------
genai.configure(api_key="AIzaSyDiVYyJztU8LaUlj4rJkOYvg8yGBBAfvLU")

# ====== VIDEO CONFIGURATION ======
VIDEO_DOMAINS = {
    'reddit.com', 'v.redd.it', 'youtube.com', 'youtu.be',
    'streamable.com', 'gfycat.com', 'imgur.com', 'tiktok.com',
    'instagram.com', 'twitter.com', 'x.com', 'twitch.tv',
    'dailymotion.com', 'rumble.com', 'foxnews.com', 'nypost.com',
    'breitbart.com', 'dailywire.com', 'newsmax.com', 'thegatewaypundit.com'
}

BACKUP_SUBREDDITS = [
    "Conservative", 
    "Republicans",
    "ConservativeMemes", 
    "ConservativeOnly", 
    "AskThe_Donald", 
    "TheTrumpZone"
]

def is_supported_video(url):
    return any(domain in url for domain in VIDEO_DOMAINS)

def get_video_resolution(video_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=s=x:p=0',
            video_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            width_str, height_str = result.stdout.strip().split('x')
            return int(width_str), int(height_str)
    except Exception as e:
        print(f"⚠️ Error getting resolution: {e}")
    return None, None

# ====== NEW FUNCTION: Adjust Video Speed ======
def adjust_speed(video_path, speed_factor=1.2):
    try:
        output_path = video_path.replace(".mp4", "_spedup.mp4")
        cmd = [
            'ffmpeg', '-i', video_path,
            '-filter_complex', f'[0:v]setpts=(1/{speed_factor})*PTS[v];[0:a]atempo={speed_factor}[a]',
            '-map', '[v]', '-map', '[a]',
            '-y', output_path
        ]
        subprocess.run(cmd, check=True)
        return output_path
    except Exception as e:
        print(f"❌ Speed adjustment failed: {str(e)}")
        return None

# ====== CORE FUNCTIONS ======
def download_video(url, retries=2):
    """Download video with retry logic. Files are saved in the current working directory."""
    for attempt in range(retries + 1):
        try:
            ydl_opts = {
                'outtmpl': '%(title)s.%(ext)s',
                'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'referer': url,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                                  'Chrome/122.0.0.0 Safari/537.36',
                    'Accept-Language': 'en-US,en;q=0.9'
                },
                'extractor_args': {
                    'youtube': {'skip': ['dash', 'hls']},
                    'twitter': {'include': ['native_video']}
                }
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info:
                    print(f"⚠️ Skipping: Playlist or channel detected with {len(info['entries'])} videos")
                    return None, 0
                ydl.download([url])
                downloaded_file = ydl.prepare_filename(info)
                
                if not os.path.exists(downloaded_file):
                    print("⚠️ Download failed: File not found")
                    if attempt < retries:
                        print(f"Retrying download ({attempt+1}/{retries})...")
                        time.sleep(2)
                        continue
                    return None, 0
                
                result = subprocess.run(
                    [
                        'ffprobe', '-loglevel', 'error',
                        '-select_streams', 'a',
                        '-show_entries', 'stream=codec_type',
                        '-of', 'csv=p=0', downloaded_file
                    ],
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
            if attempt < retries:
                print(f"Retrying download ({attempt+1}/{retries})...")
                time.sleep(2)
            else:
                return None, 0

def check_embedded_video(post):
    try:
        if hasattr(post, 'media') and post.media:
            if 'reddit_video' in post.media:
                return post.media['reddit_video']['fallback_url']
        if hasattr(post, 'preview') and 'reddit_video_preview' in post.preview:
            return post.preview['reddit_video_preview']['fallback_url']
        if hasattr(post, 'crosspost_parent_list') and post.crosspost_parent_list:
            parent = post.crosspost_parent_list[0]
            if 'media' in parent and parent['media'] and 'reddit_video' in parent['media']:
                return parent['media']['reddit_video']['fallback_url']
    except Exception as e:
        print(f"Error checking for embedded video: {e}")
    return None

def convert_to_tiktok(video_path):
    try:
        output_path = video_path.replace(".mp4", "_VERTICAL.mp4")
        width, height = get_video_resolution(video_path)
        if not width or not height:
            print("⚠️ Could not determine video resolution; skipping conversion.")
            return None
        
        aspect_ratio = width / height
        target_ratio = 9 / 16
        tolerance = 0.02

        if abs(aspect_ratio - target_ratio) < tolerance:
            print("Video is already near 9:16. Using simple scale.")
            cmd = [
                'ffmpeg', '-i', video_path,
                '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1',
                '-c:a', 'copy',
                '-y', output_path
            ]
        else:
            print("Video is not 9:16. Applying blur background.")
            cmd = [
                'ffmpeg', '-i', video_path,
                '-vf', (
                    'split [original][blur];'
                    '[blur] scale=1080:1920, gblur=sigma=20, setsar=1 [bg];'
                    '[original] scale=1080:1080:force_original_aspect_ratio=increase,'
                    'crop=1080:1080:exact=1 [scaled];'
                    '[bg][scaled] overlay=(W-w)/2:(H-h)/2:format=auto,'
                    'setdar=9/16,setsar=1'
                ),
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
        text = re.sub(r'[^\w\s\'".,!?-]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        model = genai.GenerativeModel("gemini-pro")
        response = model.generate_content(
            f"Create a viral social media caption from this Reddit title. You need to read the title and comprehend it first. Figure out what the genre is (baseball, basketball, fashion, animals, comedy, politics, fitness, etc.) so that your new title is related to the video. "
            f"Follow these rules:\n"
            f"1. Remove brackets but keep quoted names; NO EMOJIS\n"
            f"2. 8-12 words max\n"
            f"3. Attention-grabbing phrasing\n"
            f"4. Add context if needed\n"
            f"5. IMPORTANT: Use ONLY basic Latin alphabet characters (a-z, A-Z) and basic punctuation (.,!?'\"). NO special characters.\n\n"
            f"Example transformations:\n"
            f"- '[NFL Films] Zack Baun's play...' → 'Zack Baun: Iconic Defensive Move'\n"
            f"- 'Coach says \"Need better defense\"' → 'Coach Demands Defensive Improvement'\n\n"
            f"Original text: {text}"
        )
        
        caption = response.text.strip() or " ".join(text.split()[:12])
        caption = re.sub(r'[^\w\s\'".,!?-]', '', caption)
        words = caption.split()
        if len(words) > 12:
            caption = " ".join(words[:12])
        return caption
    except Exception as e:
        print(f"⚠️ AI caption failed: {str(e)}")
        simple_text = re.sub(r'[^\w\s\'".,!?-]', '', text)
        return " ".join(simple_text.split()[:12])

def add_caption(video_path, text):
    try:
        caption = generate_short_caption(text)
        caption = caption.replace("'", "'\\''")
        wrapped_text = textwrap.fill(caption, width=28, break_long_words=True, break_on_hyphens=False)
        
        final_output = video_path.replace(".mp4", "_FINAL.mp4")
        cmd = [
            'ffmpeg', '-y', '-i', video_path,
            '-vf', (
                f"drawtext=text='{wrapped_text}':"
                "fontfile=/Library/Fonts/ProximaNova-ExtraBold.ttf:"
                "fontsize=58:fontcolor=white:"
                "bordercolor=black:borderw=5:"
                "x=(w-tw)/2:y=h/7:text_align=center"
            ),
            '-c:a', 'copy',
            final_output
        ]
        
        print(f"Adding caption: '{caption}'")
        subprocess.run(cmd, check=True)
        return final_output
    except Exception as e:
        print(f"❌ Caption failed: {str(e)}")
        try:
            simple_caption = "Republican News"
            final_output = video_path.replace(".mp4", "_FINAL.mp4")
            cmd = [
                'ffmpeg', '-y', '-i', video_path,
                '-vf', (
                    f"drawtext=text='{simple_caption}':"
                    "fontfile=/Library/Fonts/ProximaNova-ExtraBold.ttf:"
                    "fontsize=58:fontcolor=white:"
                    "bordercolor=black:borderw=5:"
                    "x=(w-tw)/2:y=h/7:text_align=center"
                ),
                '-c:a', 'copy',
                final_output
            ]
            subprocess.run(cmd, check=True)
            return final_output
        except:
            print("❌ Even fallback caption failed, returning video without caption")
            final_output = video_path.replace(".mp4", "_FINAL.mp4")
            os.rename(video_path, final_output)
            return final_output

def extract_video_from_article(url):
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        video_tags = soup.find_all('video')
        if video_tags:
            for video in video_tags:
                if video.has_attr('src'):
                    return video['src']
                source = video.find('source')
                if source and source.has_attr('src'):
                    return source['src']
        
        iframes = soup.find_all('iframe')
        for iframe in iframes:
            if iframe.has_attr('src'):
                src = iframe['src']
                if any(domain in src for domain in ['youtube.com', 'youtu.be', 'twitter.com', 'x.com']):
                    return src
        return None
    except Exception as e:
        print(f"Error extracting video from article: {e}")
        return None

# ====== MODIFIED PROCESS SUBREDDIT FUNCTION ======
def process_subreddit(subreddit_name, time_filter="day", limit=100, target=5, min_target=3, processed=0, processed_urls=set(), drive_service=None, drive_folder_id=None):
    print(f"\n🔍 Searching r/{subreddit_name} for videos ({time_filter})...")
    
    for post in reddit.subreddit(subreddit_name).top(time_filter=time_filter, limit=limit):
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            video_url = post.url
            
            if not is_supported_video(video_url):
                embedded_url = check_embedded_video(post)
                if embedded_url:
                    print(f"Found embedded video: {embedded_url}")
                    video_url = embedded_url
                elif any(domain in video_url for domain in ['foxnews.com', 'nypost.com', 'breitbart.com']):
                    extracted_url = extract_video_from_article(video_url)
                    if extracted_url:
                        print(f"Extracted video from article: {extracted_url}")
                        video_url = extracted_url
                    else:
                        print(f"⚠️ Skipping: No video found in article - {video_url}")
                        continue
                else:
                    print(f"⚠️ Skipping: Unsupported URL - {video_url}")
                    continue
            
            if video_url in processed_urls:
                print(f"⚠️ Skipping: Video already processed - {video_url}")
                continue
            
            video_path, duration = download_video(video_url)
            if not video_path:
                continue
                
            if not (10 <= duration <= 180):
                print(f"⚠️ Skipping: Duration {duration}s out of range (10-180s)")
                os.remove(video_path)
                continue
            
            if subreddit_name != "Republican":
                sped_up_path = adjust_speed(video_path)
                if sped_up_path:
                    os.remove(video_path)
                    video_path = sped_up_path
                else:
                    print("⚠️ Skipping: Speed adjustment failed")
                    os.remove(video_path)
                    continue
            
            vertical_path = convert_to_tiktok(video_path)
            os.remove(video_path)
            if not vertical_path:
                continue
            
            final_path = add_caption(vertical_path, post.title)
            os.remove(vertical_path)
            if final_path:
                processed += 1
                processed_urls.add(video_url)
                print(f"✅ Success: {os.path.basename(final_path)}")
                # Upload final video to Google Drive folder "Poly"
                upload_to_drive(drive_service, drive_folder_id, final_path)
                os.remove(final_path)
            
        except Exception as e:
            print(f"⚠️ Error processing: {str(e)}")
    
    return processed

# ====== MAIN PROCESS ======
if __name__ == "__main__":
    processed = 0
    target = 5
    min_target = 3
    processed_urls = set()

    # Authenticate to Google Drive and get/create folder "Poly"
    drive_service = authenticate_drive()
    drive_folder_id = get_or_create_folder(drive_service, "Poly")
    
    print("\n" + "="*40)
    print("🚀 Processing videos from Republican subreddits")
    print("="*40)

    processed = process_subreddit("Republican", "day", 100, target, min_target, processed, processed_urls, drive_service, drive_folder_id)
    
    if processed < min_target:
        for backup_sub in BACKUP_SUBREDDITS:
            if processed >= min_target:
                break
            processed = process_subreddit(backup_sub, "day", 50, target, min_target, processed, processed_urls, drive_service, drive_folder_id)

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} videos processed")
