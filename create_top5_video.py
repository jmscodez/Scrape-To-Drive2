import os
import re
import json
import random
import requests
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont
import ffmpeg

# ========= CONFIG ===========
SHEET_ID = '1NR_UyXshaiJ9X2XFdVPpch3fpdJZUq6qLmGeMesUMrQ'
SHEET_TAB = 'Top_5_Master'
DOWNLOAD_DIR = './downloads'
OUT_VIDEO = './final.mp4'
GDRIVE_PARENT = 'impulse'
GDRIVE_FOLDER = 'Top 5'

# Google Service Account AUTH (for both Sheets and Drive)
creds_json = os.getenv('GDRIVE_SERVICE_ACCOUNT')
creds = Credentials.from_service_account_info(json.loads(creds_json),
    scopes=[
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/spreadsheets'
    ])
gc = gspread.authorize(creds)
import googleapiclient.discovery
drive_service = googleapiclient.discovery.build('drive', 'v3', credentials=creds)

def ensure_drive_folder(parent, child):
    def find_folder(name, parent_id):
        result = drive_service.files().list(q=f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{name}' and '{parent_id}' in parents", fields="files(id)").execute()
        files = result.get('files', [])
        return files[0]['id'] if files else None
    root = find_folder(parent, 'root') or drive_service.files().create(body={'name': parent, 'mimeType': 'application/vnd.google-apps.folder', 'parents': ['root']}).execute()['id']
    child_id = find_folder(child, root) or drive_service.files().create(body={'name': child, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [root]}).execute()['id']
    return child_id

def update_sheet_used(row_index):
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_TAB)
    ws.update_cell(row_index+2, 6, "Yes")  # 1-based with header row

def get_next_unused_idea():
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_TAB)
    rows = ws.get_all_records()
    unused = [i for i, r in enumerate(rows) if r['Used?'].strip().lower() != 'yes']
    if not unused:
        raise Exception("No unused ideas left!")
    idx = random.choice(unused)
    return rows[idx], idx

def get_suggestions(row):
    return [row[f'Suggestion {i}'] for i in range(1, 11) if row.get(f'Suggestion {i}', '').strip()]

def search_highlightly(query, sport):
    # Only works for NFL, as per current RapidAPI docs!
    base_url = "https://sport-highlights-api.p.rapidapi.com/american-football/highlights"
    params = {
        "leagueType": "NFL",   # NCAA? Change here.
        "limit": 40
    }
    headers = {
        "x-rapidapi-key": os.getenv("RAPIDAPI_KEY"),
        "x-rapidapi-host": "sport-highlights-api.p.rapidapi.com"
    }
    resp = requests.get(base_url, headers=headers, params=params)
    try:
        results = resp.json()
    except Exception:
        print("API error or bad response:", resp.status_code, resp.text[:500])
        return None
    # Filter for matching query in title
    for item in results.get("highlights", []):
        if query.lower() in item.get("title", "").lower():
            return item.get("url") or item.get("videoUrl") or item.get("mediaUrl")
    # Fallback: just return first highlight
    if results.get("highlights"):
        return results["highlights"][0].get("url") or results["highlights"][0].get("videoUrl") or results["highlights"][0].get("mediaUrl")
    return None

def download_video(url, outname):
    resp = requests.get(url, stream=True)
    with open(outname, 'wb') as f:
        for chunk in resp.iter_content(1024*1024):
            f.write(chunk)

def make_text_overlay(text, filename, size=(1280, 160), fontsize=70):
    try:
        font = ImageFont.truetype("arial.ttf", fontsize)
    except:
        font = ImageFont.load_default()
    img = Image.new("RGBA", size, (0,0,0,180))
    draw = ImageDraw.Draw(img)
    draw.text((40,30), text, font=font, fill=(255,255,255,255))
    img.save(filename)

def combine_clips_with_overlays(rank_titles, clips, outname):
    overlays = []
    for i, (rank, clip) in enumerate(zip(rank_titles, clips)):
        txt = f"{rank}"
        img_overlay = f"overlay_{i+1}.png"
        make_text_overlay(txt, img_overlay)
        overlays.append(img_overlay)
        tmp = f'overlayed_{i+1}.mp4'
        (
            ffmpeg
            .input(clip)
            .output(tmp, vf=f"movie={img_overlay} [ol]; [in][ol] overlay=0:0 [out]", vcodec="libx264", acodec="copy")
            .overwrite_output()
            .run()
        )
        clips[i] = tmp
    # Concatenate
    txtfile = "inputs.txt"
    with open(txtfile, 'w') as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    ffmpeg.input(txtfile, format='concat', safe=0).output(outname, c='copy').overwrite_output().run()

def upload_to_drive(filepath, folder_id):
    from googleapiclient.http import MediaFileUpload
    file_metadata = {
        'name': os.path.basename(filepath),
        'parents': [folder_id]
    }
    media = MediaFileUpload(filepath, mimetype='video/mp4')
    drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def main():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
    row, rowidx = get_next_unused_idea()
    title = row['Title']
    sport = row['Sport']
    suggestions = get_suggestions(row)
    print("Selected:", title)
    highlight_urls = []
    for q in suggestions:
        url = search_highlightly(q, sport)
        if url and url not in highlight_urls:
            highlight_urls.append(url)
        if len(highlight_urls) >= 5:
            break
    attempts = 0
    while len(highlight_urls) < 5 and attempts < 5:
        generic = search_highlightly(title, sport)
        if generic and generic not in highlight_urls:
            highlight_urls.append(generic)
        attempts += 1
    if len(highlight_urls) < 5:
        raise Exception("Could not find 5 highlights for this topic.")
    filenames = []
    for idx, url in enumerate(highlight_urls):
        fname = os.path.join(DOWNLOAD_DIR, f"highlight_{idx+1}.mp4")
        download_video(url, fname)
        filenames.append(fname)
    print("Downloaded highlight clips.")

    rank_titles = [f"#{i+1}" for i in range(5)]
    combine_clips_with_overlays(rank_titles, filenames, OUT_VIDEO)
    print("Video composed.")

    update_sheet_used(rowidx)

    folder_id = ensure_drive_folder(GDRIVE_PARENT, GDRIVE_FOLDER)
    upload_to_drive(OUT_VIDEO, folder_id)
    print("Uploaded to Google Drive.")

if __name__ == "__main__":
    main()
