# poly.py
import os
import sys
import datetime
import subprocess
import json
import requests
import shutil
import re
import time

# Attempt to import snscrape, provide guidance if it fails
try:
    import snscrape.modules.twitter as sntwitter
except ImportError:
    print("Error: snscrape library not found.")
    print("Please install it: pip install git+https://github.com/JustAnotherArchivist/snscrape.git")
    # Alternatively, if the user wants to stick to official releases (potentially less up-to-date):
    # print("Please install it: pip install snscrape")
    sys.exit(1)

import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# --- Constants ---
ACCOUNTS = ["disclosetv", "CollinRugg", "MarioNawfal"]
OPENROUTER_MODEL = "google/gemini-2.0-flash-lite-001" # Note: Model requested google/gemini-2.0-flash-lite-001 does not seem to exist, using google/gemini-flash-1.5 instead
GDRIVE_FOLDER_NAME = "Poly"
MAX_VIDEOS_TO_UPLOAD = 5
VIDEO_MIN_DURATION = 10  # seconds
VIDEO_MAX_DURATION = 180 # seconds
TEMP_DIR = "temp_poly_processing"
SERVICE_ACCOUNT_FILE = os.path.join(TEMP_DIR, "gdrive_service_account.json")
COOKIES_FILE = "cookies.txt" # Assumes cookies.txt is in the repo root

# --- Environment Variable Check ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GDRIVE_SERVICE_ACCOUNT_JSON = os.environ.get("GDRIVE_SERVICE_ACCOUNT")

if not OPENROUTER_API_KEY:
    print("Error: OPENROUTER_API_KEY environment variable not set.")
    sys.exit(1)
if not GDRIVE_SERVICE_ACCOUNT_JSON:
    print("Error: GDRIVE_SERVICE_ACCOUNT environment variable not set.")
    sys.exit(1)

# --- Helper Functions ---

def run_command(command, check=True, capture_output=False, text=True):
    """Runs a subprocess command."""
    print(f"Running command: {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=text,
            stderr=subprocess.PIPE if not capture_output else None # Capture stderr separately if not capturing all output
        )
        if result.returncode != 0:
            print(f"Command failed with error:\n{result.stderr}")
            if check:
                raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout, stderr=result.stderr)
        return result
    except FileNotFoundError:
        print(f"Error: Command not found: {command[0]}. Is it installed and in PATH?")
        raise
    except subprocess.CalledProcessError as e:
        print(f"Command '{' '.join(e.cmd)}' failed with return code {e.returncode}")
        if e.stderr:
            print(f"Stderr:\n{e.stderr}")
        if e.stdout:
             print(f"Stdout:\n{e.stdout}")
        if check:
             raise
        return e # Return the exception object if check=False

def setup_temp_dir():
    """Creates the temporary directory."""
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"Created temporary directory: {TEMP_DIR}")
    # Write the service account JSON to a file
    try:
        with open(SERVICE_ACCOUNT_FILE, 'w') as f:
            f.write(GDRIVE_SERVICE_ACCOUNT_JSON)
        print(f"Service account JSON written to {SERVICE_ACCOUNT_FILE}")
    except Exception as e:
        print(f"Error writing service account JSON to file: {e}")
        sys.exit(1)


def cleanup_temp_dir():
    """Removes the temporary directory."""
    if os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR)
            print(f"Cleaned up temporary directory: {TEMP_DIR}")
        except OSError as e:
            print(f"Error removing temporary directory {TEMP_DIR}: {e}")

def get_time_window():
    """Computes the 'yesterday 00:00 UTC to today 00:00 UTC' window."""
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    yesterday_utc = today_utc - datetime.timedelta(days=1)
    since_date = yesterday_utc.strftime("%Y-%m-%d")
    until_date = today_utc.strftime("%Y-%m-%d")
    print(f"Time window: {since_date} 00:00 UTC -> {until_date} 00:00 UTC")
    return since_date, until_date

def scrape_tweets(accounts, since_date, until_date):
    """Scrapes tweets with videos from specified accounts within the time window."""
    all_tweets = []
    print(f"Starting tweet scraping for accounts: {', '.join(accounts)}")
    for account in accounts:
        query = f"from:{account} since:{since_date} until:{until_date} filter:videos"
        print(f"Running query: {query}")
        try:
            scraper = sntwitter.TwitterSearchScraper(query)
            count = 0
            for tweet in scraper.get_items():
                # Basic check if rawContent contains common video domain patterns
                content_lower = tweet.rawContent.lower() if tweet.rawContent else ""
                has_video_link = "t.co/" in content_lower or "pic.twitter.com/" in content_lower or "video.twimg.com" in content_lower

                # Note: snscrape doesn't reliably tell us *if* a video exists, only that the filter:videos flag was used.
                # We rely on yt-dlp later to confirm. Adding a basic check for t.co links which often indicate media.
                if tweet.url and tweet.rawContent and has_video_link:
                    all_tweets.append({
                        "url": tweet.url,
                        "text": tweet.rawContent,
                        "account": account
                    })
                    count += 1
            print(f"Found {count} potential video tweets for @{account}")
        except Exception as e:
            print(f"Error scraping @{account}: {e}")
            print("snscrape can be unreliable due to Twitter changes. Consider alternatives if issues persist.")
            # Continue to the next account even if one fails
    print(f"Total potential video tweets collected: {len(all_tweets)}")
    return all_tweets

def openrouter_request(messages, max_tokens=50, temperature=0.7):
    """Sends a request to the OpenRouter API."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60) # Increased timeout
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling OpenRouter API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response status code: {e.response.status_code}")
            try:
                print(f"Response body: {e.response.json()}")
            except json.JSONDecodeError:
                 print(f"Response body (non-JSON): {e.response.text}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during OpenRouter request: {e}")
        return None

def score_tweet(tweet_text):
    """Scores a tweet's relevance using OpenRouter."""
    prompt = (
        "Rate the following tweet on a scale from 1 to 10 for its relevance to today's major U.S. political news stories. "
        "Score 0 if the content is primarily violent, hateful, or focuses solely on a controversial figure known mainly for non-political controversy (e.g., Andrew Tate). "
        "Political figures (e.g., Trump, Biden) and significant global events impacting the US are relevant. "
        f"Tweet content: \"{tweet_text}\". Respond with only the numeric score (1-10 or 0)."
    )
    messages = [
        {"role": "system", "content": "You are an AI assistant that provides only a single numeric score based on the user's criteria."},
        {"role": "user", "content": prompt}
    ]
    result = openrouter_request(messages, max_tokens=5, temperature=0.1)

    if result and result.get("choices") and result["choices"][0].get("message"):
        try:
            score_str = result["choices"][0]["message"]["content"].strip()
            # Extract the first number found
            match = re.search(r'\d+', score_str)
            if match:
                score = int(match.group(0))
                return max(0, min(10, score)) # Clamp score between 0 and 10
            else:
                print(f"Could not parse score from OpenRouter response: {score_str}")
                return 0
        except (ValueError, TypeError, KeyError, IndexError) as e:
            print(f"Error processing score from OpenRouter response: {e}. Response: {result}")
            return 0
    else:
        print("Failed to get valid score from OpenRouter.")
        return 0

def download_video(tweet_url, output_template):
    """Downloads video using yt-dlp."""
    print(f"Attempting to download video from: {tweet_url}")
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4
        'merge_output_format': 'mp4',
        'forceipv4': True,
        'quiet': False, # Set to False for more download details
        'no_warnings': True,
        'retries': 3,
        'socket_timeout': 60, # Increased timeout
        # Attempt to use cookies if the file exists
        'cookiefile': COOKIES_FILE if os.path.exists(COOKIES_FILE) else None,
        'verbose': False # Set to True for maximum debugging output from yt-dlp
    }
    downloaded_path = None
    duration = 0
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(tweet_url, download=True)
            # yt-dlp might choose a different extension, find the actual downloaded file
            # The filename is usually based on the template before merging
            base_path = ydl.prepare_filename(info_dict).rsplit('.', 1)[0]
            potential_path_mp4 = base_path + ".mp4"

            if os.path.exists(potential_path_mp4):
                 downloaded_path = potential_path_mp4
            else:
                 # Fallback if merged format wasn't mp4 for some reason (less likely with current opts)
                 print(f"Warning: Expected MP4 not found at {potential_path_mp4}, searching for other extensions...")
                 temp_dir_content = os.listdir(TEMP_DIR)
                 vid_id = info_dict.get('id', 'unknown_id') # Get ID from info_dict if possible
                 for fname in temp_dir_content:
                      if fname.startswith(f"temp_{vid_id}") and fname.endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')): # Check common video extensions
                           downloaded_path = os.path.join(TEMP_DIR, fname)
                           print(f"Found downloaded file: {downloaded_path}")
                           break
                 if not downloaded_path:
                      print(f"Could not locate downloaded video file starting with 'temp_{vid_id}' in {TEMP_DIR}")
                      return None, 0

            duration = info_dict.get('duration', 0)
            print(f"Download successful: {downloaded_path}, Duration: {duration}s")

    except yt_dlp.utils.DownloadError as e:
        print(f"yt-dlp download error for {tweet_url}: {e}")
        # Clean up potentially partially downloaded files based on template
        base_template = output_template.split('%')[0]
        for item in os.listdir(TEMP_DIR):
             if item.startswith(os.path.basename(base_template)):
                  try:
                       item_path = os.path.join(TEMP_DIR, item)
                       if os.path.isfile(item_path):
                            os.remove(item_path)
                       elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                       print(f"Cleaned up partial download artifact: {item}")
                  except OSError as clean_err:
                       print(f"Warning: Could not clean up partial download {item}: {clean_err}")

        return None, 0
    except Exception as e:
        print(f"Unexpected error during video download for {tweet_url}: {e}")
        return None, 0

    return downloaded_path, duration


def check_video_properties(video_path):
    """Checks if video has audio and meets duration constraints using ffprobe."""
    print(f"Checking properties for: {video_path}")
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=codec_type",
        "-show_entries", "format=duration",
        "-of", "json",
        video_path
    ]
    try:
        result = run_command(command, check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)

        has_audio = any(stream.get('codec_type') == 'audio' for stream in data.get('streams', []))
        duration_str = data.get('format', {}).get('duration')

        if not duration_str:
            print("Error: Could not extract duration.")
            return False
        duration = float(duration_str)

        if not has_audio:
            print("Video filter failed: No audio track detected.")
            return False
        if not (VIDEO_MIN_DURATION <= duration <= VIDEO_MAX_DURATION):
            print(f"Video filter failed: Duration {duration:.2f}s is outside the allowed range ({VIDEO_MIN_DURATION}-{VIDEO_MAX_DURATION}s).")
            return False

        print(f"Video properties OK (has audio, duration {duration:.2f}s).")
        return True
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, KeyError, FileNotFoundError) as e:
        print(f"Error checking video properties with ffprobe for {video_path}: {e}")
        return False

def convert_to_portrait(input_path, output_path):
    """Converts video to 9:16 portrait format with black bars using ffmpeg."""
    print(f"Converting {os.path.basename(input_path)} to 9:16 portrait...")
    filter_chain = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
    command = [
        "ffmpeg",
        "-y",           # Overwrite output files without asking
        "-i", input_path,
        "-vf", filter_chain,
        "-c:a", "copy", # Copy audio stream without re-encoding
        "-preset", "veryfast", # Faster encoding, potentially larger file size/lower quality tradeoff
        "-loglevel", "warning", # Reduce ffmpeg log verbosity
        output_path
    ]
    try:
        run_command(command, check=True)
        print(f"Conversion successful: {os.path.basename(output_path)}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error converting video to portrait for {input_path}: {e}")
        return False

def generate_headline(tweet_text):
    """Generates a concise headline using OpenRouter."""
    prompt = (
        "Generate a very concise headline (less than 10 words, no hashtags) summarizing the following tweet content. "
        "The headline should be suitable for use as a filename (avoid special characters). "
        f"Tweet content: \"{tweet_text}\""
    )
    messages = [
        {"role": "system", "content": "You create short, filename-safe headlines under 10 words without hashtags."},
        {"role": "user", "content": prompt}
    ]
    result = openrouter_request(messages, max_tokens=30, temperature=0.5)

    if result and result.get("choices") and result["choices"][0].get("message"):
        try:
            headline = result["choices"][0]["message"]["content"].strip()
            # Sanitize for filename
            headline = re.sub(r'[\\/*?:"<>|]', '', headline) # Remove invalid filename chars
            headline = re.sub(r'\s+', ' ', headline).strip() # Normalize whitespace
            # Truncate to roughly 9 words if needed (split and join)
            words = headline.split()
            if len(words) >= 10:
                headline = " ".join(words[:9])
            print(f"Generated headline: {headline}")
            return headline if headline else f"video_{int(time.time())}" # Fallback name
        except (KeyError, IndexError) as e:
            print(f"Error processing headline from OpenRouter response: {e}. Response: {result}")
            return f"video_{int(time.time())}" # Fallback name
    else:
        print("Failed to generate headline from OpenRouter.")
        return f"video_{int(time.time())}" # Fallback name

def get_drive_service():
    """Authenticates and returns the Google Drive API service object."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds, cache_discovery=False) # Disable discovery cache
        print("Google Drive service authenticated successfully.")
        return service
    except FileNotFoundError:
        print(f"Error: Service account file not found at {SERVICE_ACCOUNT_FILE}")
        return None
    except Exception as e:
        print(f"Error creating Google Drive service: {e}")
        return None

def find_or_create_folder(service, folder_name):
    """Finds a folder by name or creates it if it doesn't exist."""
    try:
        # Search for the folder
        query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        folders = response.get('files', [])

        if folders:
            folder_id = folders[0]['id']
            print(f"Found folder '{folder_name}' with ID: {folder_id}")
            return folder_id
        else:
            # Create the folder
            print(f"Folder '{folder_name}' not found, creating it...")
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            folder_id = folder.get('id')
            print(f"Created folder '{folder_name}' with ID: {folder_id}")
            return folder_id
    except HttpError as e:
        print(f"An API error occurred while finding/creating folder '{folder_name}': {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during folder check/creation: {e}")
        return None


def upload_to_drive(service, folder_id, file_path, file_name):
    """Uploads a file to the specified Google Drive folder."""
    print(f"Uploading '{file_name}' to Google Drive folder ID {folder_id}...")
    try:
        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        )
        response = None
        upload_start_time = time.time()
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    print(f"Upload progress: {int(status.progress() * 100)}%")
            except HttpError as e:
                 if e.resp.status in [500, 502, 503, 504]:
                      print(f"Resumable upload error (HTTP {e.resp.status}), retrying: {e}")
                      time.sleep(5) # Wait before retrying
                      # Recreate request might be needed for certain errors, but start simple
                      continue # Retry the next_chunk call
                 else:
                      print(f"An non-retriable API error occurred during upload: {e}")
                      return False # Non-retriable error
            except Exception as e:
                 print(f"An unexpected error occurred during resumable upload chunk: {e}")
                 return False # Unexpected error

        upload_duration = time.time() - upload_start_time
        print(f"File '{file_name}' uploaded successfully (ID: {response.get('id')}) in {upload_duration:.2f} seconds.")
        return True
    except HttpError as e:
        print(f"An API error occurred during upload initiation: {e}")
        return False
    except FileNotFoundError:
        print(f"Error: File not found for upload: {file_path}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during file upload: {e}")
        return False

# --- Main Logic ---

def main():
    setup_temp_dir()
    successful_uploads = 0
    processed_tweet_urls = set() # Track URLs to avoid re-processing identical tweets if scraped multiple times

    try:
        since_date, until_date = get_time_window()
        tweets = scrape_tweets(ACCOUNTS, since_date, until_date)

        if not tweets:
            print("No tweets found matching the criteria. Exiting.")
            return

        # Score tweets
        scored_tweets = []
        print("\nScoring tweets...")
        for tweet in tweets:
             # Skip if already processed (can happen if scraped via multiple terms or accounts)
             if tweet['url'] in processed_tweet_urls:
                  continue

             print(f"Scoring: {tweet['url']} | Text: {tweet['text'][:100]}...")
             score = score_tweet(tweet['text'])
             print(f"Score: {score}")
             scored_tweets.append({**tweet, 'score': score})
             processed_tweet_urls.add(tweet['url']) # Mark as processed

        # Sort by score and keep top N (or fewer if less scored)
        scored_tweets.sort(key=lambda x: x['score'], reverse=True)
        top_tweets = scored_tweets[:MAX_VIDEOS_TO_UPLOAD * 2] # Get more than needed initially to allow for failures
        print(f"\nTop {len(top_tweets)} tweets after scoring (sorted):")
        for i, tweet in enumerate(top_tweets):
             print(f"{i+1}. Score: {tweet['score']} | URL: {tweet['url']}")


        print(f"\nProcessing top {MAX_VIDEOS_TO_UPLOAD} tweets for video download and upload...")
        drive_service = get_drive_service()
        if not drive_service:
            print("Failed to authenticate Google Drive service. Cannot upload.")
            return # Exit if Drive service fails early

        drive_folder_id = find_or_create_folder(drive_service, GDRIVE_FOLDER_NAME)
        if not drive_folder_id:
            print("Failed to find or create Google Drive folder. Cannot upload.")
            return # Exit if folder is not available

        # --- Video Processing Loop ---
        for tweet in top_tweets:
            if successful_uploads >= MAX_VIDEOS_TO_UPLOAD:
                print(f"Reached target of {MAX_VIDEOS_TO_UPLOAD} successful uploads.")
                break

            print(f"\n--- Processing Tweet: {tweet['url']} (Score: {tweet['score']}) ---")
            tweet_id = tweet['url'].split('/')[-1].split('?')[0] # Basic way to get an ID for temp naming
            temp_video_base = os.path.join(TEMP_DIR, f"temp_{tweet_id}")
            original_download_path = None
            converted_path = None
            final_renamed_path = None

            try:
                # 1. Download Video
                download_template = temp_video_base + ".%(ext)s"
                original_download_path, duration = download_video(tweet['url'], download_template)
                if not original_download_path or not os.path.exists(original_download_path):
                    print("Download failed or file not found. Skipping tweet.")
                    continue # Skip to next tweet

                # 2. Filter Video Properties (Audio & Duration)
                if not check_video_properties(original_download_path):
                    print("Video properties check failed. Skipping tweet.")
                    # Clean up downloaded file
                    if os.path.exists(original_download_path): os.remove(original_download_path)
                    continue # Skip to next tweet

                # 3. Convert to Portrait 9:16
                converted_path = temp_video_base + "_portrait.mp4"
                if not convert_to_portrait(original_download_path, converted_path):
                    print("Conversion to portrait failed. Skipping tweet.")
                    # Clean up original downloaded file
                    if os.path.exists(original_download_path): os.remove(original_download_path)
                    continue # Skip to next tweet
                # Delete original after successful conversion
                if os.path.exists(original_download_path):
                    os.remove(original_download_path)
                original_download_path = None # Avoid accidental deletion later

                # 4. Generate Headline
                headline = generate_headline(tweet['text'])
                if not headline:
                     print("Headline generation failed. Using default. Continuing process...")
                     headline = f"video_{tweet_id}" # Use a default if generation fails

                # 5. Rename Processed File
                final_filename_mp4 = f"{headline}.mp4"
                final_renamed_path = os.path.join(TEMP_DIR, final_filename_mp4)
                try:
                    shutil.move(converted_path, final_renamed_path)
                    print(f"Renamed video to: {final_filename_mp4}")
                    converted_path = None # Avoid accidental deletion later
                except OSError as e:
                    print(f"Failed to rename video file: {e}. Skipping tweet.")
                    # Clean up converted file if rename fails
                    if os.path.exists(converted_path): os.remove(converted_path)
                    continue # Skip to next tweet

                # 6. Upload to Google Drive
                if not upload_to_drive(drive_service, drive_folder_id, final_renamed_path, final_filename_mp4):
                    print("Google Drive upload failed. Skipping tweet.")
                    # Clean up local final file if upload fails
                    if os.path.exists(final_renamed_path): os.remove(final_renamed_path)
                    continue # Skip to next tweet

                # 7. Success for this tweet! Increment counter and clean up local file.
                successful_uploads += 1
                print(f"--- Successfully processed and uploaded video for tweet {tweet['url']} ---")
                # Clean up the final uploaded file locally
                if os.path.exists(final_renamed_path):
                     os.remove(final_renamed_path)
                final_renamed_path = None # Avoid accidental deletion later

            except Exception as e:
                print(f"\n!!! An unexpected error occurred processing tweet {tweet['url']}: {e} !!!")
                print("Attempting to clean up intermediate files for this tweet...")
                # Fallback cleanup for any step failure in the loop
                if original_download_path and os.path.exists(original_download_path):
                    try: os.remove(original_download_path)
                    except OSError as clean_err: print(f"Cleanup warning: {clean_err}")
                if converted_path and os.path.exists(converted_path):
                    try: os.remove(converted_path)
                    except OSError as clean_err: print(f"Cleanup warning: {clean_err}")
                if final_renamed_path and os.path.exists(final_renamed_path):
                    try: os.remove(final_renamed_path)
                    except OSError as clean_err: print(f"Cleanup warning: {clean_err}")
                # Continue to the next tweet
                continue

    finally:
        # Final summary and cleanup
        print(f"\n--- Workflow Summary ---")
        print(f"Total successful uploads: {successful_uploads}")
        cleanup_temp_dir() # Clean up the main temporary directory

if __name__ == "__main__":
    main()
