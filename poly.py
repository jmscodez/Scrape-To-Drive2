import os
import subprocess
import json
import requests
import datetime
import re
import tempfile
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

# Environment checks
if not os.environ.get("OPENROUTER_API_KEY") or not os.environ.get("GDRIVE_SERVICE_ACCOUNT"):
    raise ValueError("Missing required environment variables")

# Configure paths
COOKIES_FILE = "cookies.txt"
SERVICE_ACCOUNT_FILE = "service_account.json"

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(options=chrome_options)
    if os.path.exists(COOKIES_FILE):
        driver.get("https://twitter.com")
        with open(COOKIES_FILE, 'r') as f:
            cookies = json.load(f)
            for cookie in cookies:
                if 'sameSite' in cookie:
                    del cookie['sameSite']
                driver.add_cookie(cookie)
    return driver

def get_tweet_data(driver, account, since, until):
    search_url = f"https://twitter.com/search?q=from%3A{account}%20filter%3Avideos%20since%3A{since}%20until%3A{until}&src=typed_query"
    driver.get(search_url)
    tweets = []
    last_height = driver.execute_script("return document.body.scrollHeight")
    
    for _ in range(3):
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-testid='tweet']"))
            )
            articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            for article in articles:
                try:
                    url = article.find_element(By.CSS_SELECTOR, "a[href*='/status/']").get_attribute("href")
                    text = article.find_element(By.CSS_SELECTOR, "div[data-testid='tweetText']").text
                    if url not in [t['url'] for t in tweets]:
                        tweets.append({"url": url, "text": text})
                except Exception as e:
                    continue
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
        except TimeoutException:
            break
    
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
        if not subprocess.run(ydl_cmd, check=False).returncode == 0:
            return None
        
        video_path = os.path.join(tmpdir, "video.mp4")
        if not os.path.exists(video_path):
            return None

        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            video_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return None
            
        probe_data = json.loads(result.stdout)
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
    return output_path if subprocess.run(cmd, check=False).returncode == 0 else None

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
        
        folder_query = "name='Poly' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = service.files().list(q=folder_query).execute().get('files', [])
        folder_id = folders[0]['id'] if folders else service.files().create(
            body={"name": "Poly", "mimeType": "application/vnd.google-apps.folder"}, 
            fields='id'
        ).execute()['id']

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
    driver = setup_driver()
    try:
        today = datetime.datetime.utcnow().date()
        yesterday = today - datetime.timedelta(days=1)
        time_window = (yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

        accounts = ["disclosetv", "CollinRugg", "MarioNawfal"]
        all_tweets = []
        for account in accounts:
            print(f"Scraping {account}...")
            tweets = get_tweet_data(driver, account, time_window[0], time_window[1])
            print(f"Found {len(tweets)} videos")
            all_tweets.extend(tweets)

        scored = []
        for tweet in all_tweets:
            score = score_tweet(tweet['text'])
            if score > 0:
                scored.append((score, tweet['url'], tweet['text']))
        scored.sort(reverse=True, key=lambda x: x[0])
        top_tweets = scored[:5]

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
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
