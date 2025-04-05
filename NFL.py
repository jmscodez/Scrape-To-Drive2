import os
import re
import subprocess
import json
import praw
import yt_dlp
import requests
import textwrap
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

def download_video(url):
    try:
        ydl_opts = {
            'outtmpl': '%(id)s.%(ext)s',
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
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
    try:
        output_path = video_path.replace(".mp4", "_VERTICAL.mp4")
        subprocess.run([
            'ffmpeg', '-i', video_path,
            '-vf', 
            'split [original][blur];'
            '[blur] scale=1080:1920, gblur=sigma=20, setsar=1 [bg];'
            '[original] scale=1080:1080:force_original_aspect_ratio=increase,'
            'crop=1080:1080:exact=1 [scaled];'
            '[bg][scaled] overlay=(W-w)/2:(H-h)/2:format=auto,'
            'setdar=9/16,setsar=1',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-y', output_path
        ], check=True)
        return output_path
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        return None

# ------------------ OpenRouter API for Caption ------------------
def generate_headline(post_title):
    try:
        truncated_title = post_title[:200]
        prompt = (
            "Your job is to take captions that I give you and turn them into a headline that would be used on a TikTok video. "
            "I will give you the input at the end, your output should ONLY be the new title. "
            "There should be nothing else besides the caption as your output. Here are some rules to follow:\n"
            "1. It should be the text at the top or bottom of the video that explains what's happening in the video.\n"
            "2. It should be tailored for TikTok SEO.\n"
            "3. Remove any '_VERTICAL.mp4' text if present.\n"
            "4. Use a max of 2 emojis.\n"
            "5. NO HASHTAGS.\n"
            "6. If a name is included, keep the name in the caption.\n"
            "7. Make it 8-12 words max.\n"
            "8. Remove ALL brackets, if applicable, and use the name in brackets as a quote (if it's a name).\n\n"
            "Here is an example input and output:\n\n"
            "Input: '[NFL Films] His name is Baun, Zack Baun..._VERTICAL'\n"
            "Output: 'Zack Baun Shines at NFL Films 🎥🏈'\n\n"
            f"Now create a TikTok caption for this content: '{truncated_title}'"
        )
        
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-2-9b-it",
            "messages": [{
                "role": "system", 
                "content": "You are a social media expert who creates viral TikTok captions for NFL content."
            }, {
                "role": "user", 
                "content": prompt
            }],
            "max_tokens": 100,
            "temperature": 0.7
        }
        
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        
        caption = response.json()['choices'][0]['message']['content'].strip()
        caption = re.sub(r'_VERTICAL\.mp4', '', caption)
        caption = re.sub(r'#\w+', '', caption)
        return caption[:150]
        
    except Exception as e:
        print(f"⚠️ Headline generation failed: {str(e)}")
        return sanitize_filename(post_title)[:100]

def add_caption(video_path, text):
    try:
        wrapped_text = textwrap.fill(text, width=28, break_long_words=True, break_on_hyphens=False)
        output_path = video_path.replace("_VERTICAL.mp4", "_FINAL.mp4")
        subprocess.run([
            'ffmpeg', '-y', '-i', video_path,
            '-vf', f"drawtext=text='{wrapped_text}':"
                   "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                   "fontsize=58:"
                   "fontcolor=white:"
                   "bordercolor=black:"
                   "borderw=5:"
                   "x=(w-tw)/2:"
                   "y=h/12:"
                   "text_align=center",
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', output_path
        ], check=True)
        return output_path
    except Exception as e:
        print(f"❌ Caption failed: {str(e)}")
        return None

# ------------------ Main Process ------------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    user_agent="script:mybot:v1.0 (by /u/Proof_Difficulty_396)"
)

if __name__ == "__main__":
    processed = 0
    target = 5
    
    drive_service = authenticate_drive()
    folder_id = get_or_create_folder(drive_service, "NFL Videos")

    print("\n" + "="*40)
    print(f"🚀 Processing {target} videos from r/NFL")
    print("="*40)

    for post in reddit.subreddit("NFL").top(time_filter="day", limit=50):
        if processed >= target:
            break
            
        try:
            print(f"\n=== Processing: {post.title[:50]}... ===")
            
            if not any(domain in post.url for domain in VIDEO_DOMAINS):
                print(f"⚠️ Skipping: Unsupported URL - {post.url}")
                continue
                
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
            if not vertical_path:
                continue
                
            headline = generate_headline(post.title)
            final_path = add_caption(vertical_path, headline)
            os.remove(vertical_path)
            if not final_path:
                continue
            
            sanitized_headline = sanitize_filename(headline)
            final_name = f"{sanitized_headline}.mp4"
            os.rename(final_path, final_name)
            
            upload_to_drive(drive_service, folder_id, final_name)
            os.remove(final_name)
            processed += 1
            print(f"✅ Success: {sanitized_headline}")

        except Exception as e:
            print(f"⚠️ Error processing post {post.id}: {str(e)}")

    print("\n" + "="*40)
    print(f"🎉 Completed: {processed}/{target} videos processed")
    print("="*40)
