import os
import subprocess
import json
import requests
import datetime
import re
import tempfile
from googleapiclient.discovery import build
from google.oauth2 import service_account

# Environment checks
if not os.environ.get("OPENROUTER_API_KEY") or not os.environ.get("GDRIVE_SERVICE_ACCOUNT"):
    raise ValueError("Missing required environment variables")

# Configure paths
COOKIES_FILE = "cookies.txt"
SERVICE_ACCOUNT_FILE = "service_account.json"

def run_command(cmd):
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e.stderr}")
        return None

def get_tweet_data(account, since, until):
    query = f"from:{account} filter:videos since:{since} until:{until}"
    cmd = [
        "snscrape",
        "--jsonl",
        "--max-results", "50",
        "twitter-search",
        query
    ]
    output = run_command(cmd)
    if not output:
        return []
    
    tweets = []
    for line in output.split('\n'):
        try:
            tweet = json.loads(line)
            if 'media' in tweet and any(m['type'] == 'video' for m in tweet['media']):
                tweets.append({
                    "url": tweet['url'],
                    "text": tweet['content']
                })
        except (json.JSONDecodeError, KeyError):
            continue
    return tweets

def score_tweet(text):
    prompt = (
        "Score this tweet's relevance to today's US political news (1-10). "
        "Score 0 if violent or about purely controversial figures. "
        "Output only the numeric score."
    )
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": "google/gemini-2.0-flash-lite-001",
                "messages": [
                    {"role": "system", "content": "You are a political news relevance scorer"},
                    {"role": "user", "content": f"{prompt}\n\nTweet: {text}"}
                ],
                "temperature": 0.1
            }
        )
        return int(re.search(r'\d+', response.json()['choices'][0]['message']['content']).group())
    except Exception as e:
        print(f"Scoring failed: {e}")
        return 0

def download_video(url):
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_cmd = [
            "yt-dlp",
            "--cookies", COOKIES_FILE,
            "--force-ipv4",
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "-o", f"{tmpdir}/video.%(ext)s",
            url
        ]
        if not run_command(ydl_cmd):
            return None
        
        video_path = os.path.join(tmpdir, "video.mp4")
        if not os.path.exists(video_path):
            return None

        # Check duration and audio
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            video_path
        ]
        probe_output = run_command(probe_cmd)
        if not probe_output:
            return None
            
        probe_data = json.loads(probe_output)
        duration = float(probe_data['format']['duration'])
        has_audio = any(s['codec_type'] == 'audio' for s in probe_data['streams'])
        
        if 10 <= duration <= 180 and has_audio:
            return video_path
        return None

def convert_to_portrait(input_path):
    output_path = input_path.replace(".mp4", "_portrait.mp4")
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:a", "copy",
        output_path
    ]
    return output_path if run_command(cmd) else None

def generate_headline(text):
    prompt = "Create a <10 word filename-friendly headline from this tweet (no hashtags):\n\n" + text
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": "google/gemini-2.0-flash-lite-001",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            }
        )
        headline = response.json()['choices'][0]['message']['content'].strip()
        return re.sub(r'[^a-zA-Z0-9 ]', '', headline)[:50]
    except Exception as e:
        print(f"Headline generation failed: {e}")
        return "video"

def upload_to_drive(file_path, headline):
    try:
        with open(SERVICE_ACCOUNT_FILE, 'w') as f:
            f.write(os.environ['GDRIVE_SERVICE_ACCOUNT'])
        
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds)
        
        # Find/Create folder
        folder_query = "name='Poly' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = service.files().list(q=folder_query).execute().get('files', [])
        folder_id = folders[0]['id'] if folders else service.files().create(
            body={"name": "Poly", "mimeType": "application/vnd.google-apps.folder"}, 
            fields='id'
        ).execute()['id']

        # Upload file
        file_metadata = {'name': f"{headline}.mp4", 'parents': [folder_id]}
        media = MediaFileUpload(file_path, mimetype='video/mp4')
        service.files().create(body=file_metadata, media_body=media).execute()
        return True
    except Exception as e:
        print(f"Drive upload failed: {e}")
        return False
    finally:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            os.remove(SERVICE_ACCOUNT_FILE)

def main():
    # Calculate time window
    today = datetime.datetime.utcnow().date()
    yesterday = today - datetime.timedelta(days=1)
    time_window = (yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

    # Collect tweets
    accounts = ["disclosetv", "CollinRugg", "MarioNawfal"]
    all_tweets = []
    for account in accounts:
        print(f"Scraping {account}...")
        tweets = get_tweet_data(account, time_window[0], time_window[1])
        print(f"Found {len(tweets)} videos")
        all_tweets.extend(tweets)

    # Score and sort
    scored = []
    for tweet in all_tweets:
        score = score_tweet(tweet['text'])
        if score > 0:
            scored.append((score, tweet['url'], tweet['text']))
    scored.sort(reverse=True, key=lambda x: x[0])
    top_tweets = scored[:5]

    # Process top tweets
    uploaded = 0
    for score, url, text in top_tweets:
        try:
            print(f"Processing {url}")
            video_path = download_video(url)
            if not video_path:
                continue

            portrait_path = convert_to_portrait(video_path)
            if not portrait_path:
                continue

            headline = generate_headline(text)
            final_path = os.path.join(os.path.dirname(portrait_path), f"{headline}.mp4")
            os.rename(portrait_path, final_path)

            if upload_to_drive(final_path, headline):
                uploaded += 1
        except Exception as e:
            print(f"Processing failed: {e}")
        finally:
            for path in [video_path, portrait_path, final_path]:
                if path and os.path.exists(path):
                    os.remove(path)

    print(f"Uploaded {uploaded} videos")

if __name__ == "__main__":
    main()
