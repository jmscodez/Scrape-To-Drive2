#!/usr/bin/env python3
import os
import sys
import argparse
import subprocess
from pathlib import Path

import yt_dlp
from scenedetect import VideoManager, SceneManager
from scenedetect.detectors import ContentDetector
from pydub import AudioSegment, silence
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- CONFIGURABLE DEFAULTS ---
MIN_DURATION = 20    # seconds
MAX_DURATION = 60    # seconds
TOTAL_TARGET = 60    # final TikTok length in seconds
SERVICE_ACCOUNT_FILE = 'service_account.json'
SCOPES = ['https://www.googleapis.com/auth/drive.file']
DEFAULT_FOLDER_NAME = 'impulse'

# --- YOUTUBE CLIPPER FUNCTIONS ---

def download_video(url: str, out_path: Path) -> Path:
    out_path.mkdir(parents=True, exist_ok=True)
    opts = {
        'format': 'mp4[height<=720]',
        'outtmpl': str(out_path / 'input.%(ext)s'),
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return out_path / f"input.{info['ext']}"

def detect_scenes(video_path: Path) -> list[tuple[float,float]]:
    vm = VideoManager([str(video_path)])
    sm = SceneManager()
    sm.add_detector(ContentDetector())
    vm.start()
    sm.detect_scenes(frame_source=vm)
    scenes = sm.get_scene_list()
    vm.release()
    return [(s.get_seconds(), e.get_seconds()) for s,e in scenes]

def detect_audio_peaks(video_path: Path) -> list[tuple[float,float]]:
    audio = AudioSegment.from_file(video_path)
    nonsilent = silence.detect_nonsilent(
        audio,
        min_silence_len=500,
        silence_thresh=audio.dBFS - 16
    )
    return [(start/1000.0, end/1000.0) for start,end in nonsilent]

def select_segments(scenes, peaks, min_d, max_d):
    sel = []
    for s0,s1 in scenes:
        for p0,p1 in peaks:
            if p1 > s0 and p0 < s1:
                a = max(s0, p0 - 0.5)
                b = min(s1, p1 + 0.5)
                d = b - a
                if min_d <= d <= max_d:
                    sel.append((a,b))
    return sorted(set(sel), key=lambda x: x[0])

def extract_segment(video_path: Path, seg, idx: int, work_dir: Path) -> Path:
    out = work_dir / f"clip_{idx:02d}.mp4"
    cmd = [
        'ffmpeg','-y','-ss',str(seg[0]),'-to',str(seg[1]),
        '-i',str(video_path),'-c:v','libx264','-c:a','aac',
        str(out)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out

def concat_segments(clips, combined_path: Path):
    list_txt = combined_path.parent / 'clips.txt'
    with open(list_txt,'w') as f:
        for c in clips:
            f.write(f"file '{c.name}'\n")
    subprocess.run([
        'ffmpeg','-y','-f','concat','-safe','0',
        '-i',str(list_txt),'-c','copy',str(combined_path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    list_txt.unlink()

def reformat_tiktok(src: Path, dst: Path):
    subprocess.run([
        'ffmpeg','-y','-i',str(src),
        '-vf',"scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        '-c:a','copy',str(dst)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# --- GOOGLE DRIVE UPLOAD FUNCTIONS ---

def authenticate_drive():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build('drive','v3',credentials=creds)

def get_folder_id(service, folder_name):
    q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = service.files().list(q=q, fields='files(id)').execute()
    files = res.get('files',[])
    if not files:
        raise FileNotFoundError(f"Drive folder '{folder_name}' not found.")
    return files[0]['id']

def upload_file_to_folder(service, file_path, folder_id):
    name = file_path.name
    meta = {'name': name, 'parents':[folder_id]}
    media = MediaFileUpload(str(file_path), mimetype='video/mp4', resumable=True)
    file = service.files().create(body=meta, media_body=media, fields='id').execute()
    print(f"Uploaded '{name}' (ID: {file.get('id')})")

# --- MAIN ---

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--urls', nargs='+', required=True, help="YouTube URLs")
    p.add_argument('--output', default='final_tiktok.mp4', help="Output filename")
    p.add_argument('--folder', default=DEFAULT_FOLDER_NAME, help="Drive folder name")
    p.add_argument('--min', type=int, default=MIN_DURATION)
    p.add_argument('--max', type=int, default=MAX_DURATION)
    args = p.parse_args()

    workdir = Path('yt_work')
    if workdir.exists():
        for f in workdir.iterdir(): f.unlink()
    else:
        workdir.mkdir()

    clips = []
    for url in args.urls:
        print(f"Processing {url}")
        vid = download_video(url, workdir)
        scenes = detect_scenes(vid)
        peaks  = detect_audio_peaks(vid)
        segs   = select_segments(scenes, peaks, args.min, args.max)
        for i,seg in enumerate(segs,1):
            clips.append(extract_segment(vid, seg, i, workdir))

    # trim total to target
    total = 0
    chosen = []
    for c in clips:
        dur = float(subprocess.check_output([
            'ffprobe','-v','error','-show_entries','format=duration',
            '-of','default=noprint_wrappers=1:nokey=1',str(c)
        ]) or 0)
        if total + dur <= TOTAL_TARGET:
            chosen.append(c); total += dur

    if not chosen:
        print("No valid clips. Exiting."); sys.exit(1)

    combined = workdir / 'combined.mp4'
    concat_segments(chosen, combined)
    reformat_tiktok(combined, Path(args.output))
    print(f"✅ Clipper done: {args.output}")

    # upload to Drive
    drive = authenticate_drive()
    folder_id = get_folder_id(drive, args.folder)
    upload_file_to_folder(drive, Path(args.output), folder_id)

if __name__ == '__main__':
    main()
