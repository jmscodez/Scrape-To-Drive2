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

# --- CONFIGURABLE DEFAULTS ---
MIN_DURATION = 20    # seconds
MAX_DURATION = 60    # seconds
TOTAL_TARGET = 60    # final TikTok length in seconds

# --- UTILITY FUNCTIONS ---

def download_video(url: str, out_path: Path) -> Path:
    """Download YouTube video to out_path / input.mp4."""
    out_path.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        'format': 'mp4[height<=720]',
        'outtmpl': str(out_path / 'input.%(ext)s'),
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = out_path / f"input.{info['ext']}"
        return filename

def detect_scenes(video_path: Path) -> list[tuple[float,float]]:
    """Return list of (start_s, end_s) for each scene."""
    vm = VideoManager([str(video_path)])
    sm = SceneManager()
    sm.add_detector(ContentDetector())
    vm.start()
    sm.detect_scenes(frame_source=vm)
    scene_list = sm.get_scene_list()
    vm.release()
    return [(start.get_seconds(), end.get_seconds()) for start, end in scene_list]

def detect_audio_peaks(video_path: Path) -> list[tuple[float,float]]:
    """Return list of (start_s, end_s) non‑silent segments in audio."""
    audio = AudioSegment.from_file(video_path)
    nonsilent = silence.detect_nonsilent(audio,
        min_silence_len=500,   # 0.5 sec of silence
        silence_thresh=audio.dBFS - 16
    )
    # convert ms to seconds
    return [(start/1000.0, end/1000.0) for start, end in nonsilent]

def select_segments(scenes, peaks, min_d=MIN_DURATION, max_d=MAX_DURATION):
    """Return merged segments where scene and peak overlap and within duration bounds."""
    selected = []
    for s_start, s_end in scenes:
        for p_start, p_end in peaks:
            # check overlap
            if p_end > s_start and p_start < s_end:
                seg_start = max(s_start, p_start - 0.5)
                seg_end   = min(s_end,   p_end + 0.5)
                dur = seg_end - seg_start
                if min_d <= dur <= max_d:
                    selected.append((seg_start, seg_end))
    # remove duplicates and sort
    selected = sorted(set(selected), key=lambda x: x[0])
    return selected

def extract_segment(video_path: Path, seg: tuple[float,float], idx: int, work_dir: Path) -> Path:
    """Use ffmpeg to write one segment file and return its path."""
    out_file = work_dir / f"clip_{idx:02d}.mp4"
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(seg[0]),
        '-to', str(seg[1]),
        '-i', str(video_path),
        '-c:v', 'libx264', '-c:a', 'aac',
        str(out_file)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_file

def concat_segments(clips: list[Path], out_path: Path):
    """Concatenate a list of clip files into one file via ffmpeg."""
    list_file = out_path.parent / 'clips.txt'
    with open(list_file, 'w') as f:
        for clip in clips:
            f.write(f"file '{clip.name}'\n")
    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0',
        '-i', str(list_file),
        '-c', 'copy',
        str(out_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    list_file.unlink()

def reformat_tiktok(input_path: Path, output_path: Path):
    """Scale+pad to 1080×1920 9:16 TikTok format."""
    cmd = [
        'ffmpeg', '-y',
        '-i', str(input_path),
        '-vf', "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        '-c:a', 'copy',
        str(output_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# --- MAIN PROCESS ---

def main():
    p = argparse.ArgumentParser(description="Clip YouTube highlights to TikTok-ready format")
    p.add_argument('--urls', nargs='+', required=True, help="One or more YouTube URLs")
    p.add_argument('--workdir', default='yt_work', help="Temp working directory")
    p.add_argument('--output', default='final_tiktok.mp4', help="Final output file")
    p.add_argument('--min', type=int, default=MIN_DURATION, help="Min clip length (sec)")
    p.add_argument('--max', type=int, default=MAX_DURATION, help="Max clip length (sec)")
    args = p.parse_args()

    workdir = Path(args.workdir)
    if workdir.exists():
        for f in workdir.iterdir(): f.unlink()
    else:
        workdir.mkdir()

    all_clips = []
    for url in args.urls:
        print(f"→ Processing {url}")
        vid_path = download_video(url, workdir)
        scenes   = detect_scenes(vid_path)
        peaks    = detect_audio_peaks(vid_path)
        segs     = select_segments(scenes, peaks, args.min, args.max)
        for i, seg in enumerate(segs, start=1):
            clip = extract_segment(vid_path, seg, i, workdir)
            all_clips.append(clip)

    # trim total length to target
    total = 0
    chosen = []
    for clip in all_clips:
        dur = float(subprocess.check_output(
            ['ffprobe','-v','error','-show_entries','format=duration',
             '-of','default=noprint_wrappers=1:nokey=1', str(clip)]
        ) or 0)
        if total + dur <= TOTAL_TARGET:
            chosen.append(clip)
            total += dur
    if not chosen:
        print("No segments within duration limits. Exiting.")
        sys.exit(1)

    tmp_combined = workdir / 'combined.mp4'
    concat_segments(chosen, tmp_combined)
    reformat_tiktok(tmp_combined, Path(args.output))

    print(f"\n✅ Done! Output saved to {args.output}")

if __name__ == '__main__':
    main()
