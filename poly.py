# poly.py  ── global “trust‑nothing” switch ────────────────────────────────────
import ssl, urllib3
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings()

# ── rest of the script (unchanged functional code) ────────────────────────────
import os, sys, json, re, tempfile, datetime, subprocess
from pathlib import Path
import requests
from yt_dlp import YoutubeDL
from snscrape.modules.twitter import TwitterSearchScraper
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Config ───────────────────────────────────────────────────────────────────────
ACCOUNTS = ["disclosetv", "CollinRugg", "MarioNawfal"]
MAX_TO_UPLOAD = 5
MIN_DURATION, MAX_DURATION = 10, 180               # seconds
RES = (1080, 1920)                                  # portrait 9:16
OPENROUTER_URL   = "https://api.openrouter.ai/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.0-flash-lite-001"
COOKIEFILE = "cookies.txt"
DRIVE_FOLDER = "Poly"
# Helpers ──────────────────────────────────────────────────────────────────────
def _dates():
    today = datetime.date.today(); yday = today - datetime.timedelta(days=1)
    return yday.isoformat(), today.isoformat()

def fetch():
    since, until = _dates(); out=[]
    for a in ACCOUNTS:
        q=f"from:{a} since:{since} until:{until} filter:videos"
        for t in TwitterSearchScraper(q).get_items():
            out.append((t.url, t.content))
    return out

def score(txt,key):
    prompt=("Identify today's top US news. Score tweet 1‑10 for relevance; "
            "0 if only violent or controversial celeb. "
            f'Tweet: "{txt}" – just the number.')
    r=requests.post(OPENROUTER_URL,headers={"Authorization":f"Bearer {key}"},
        json={"model":OPENROUTER_MODEL,"messages":[{"role":"user","content":prompt}]})
    r.raise_for_status()
    return float(re.match(r"\d+(?:\.\d+)?", r.json()["choices"][0]["message"]["content"]).group())

def dl(url,d): y=YoutubeDL({"format":"bv+ba/best","cookiefile":COOKIEFILE,
    "forceipv4":True,"outtmpl":str(d/"%(id)s.%(ext)s"),"quiet":True})
    info=y.extract_info(url,download=True); return d/f"{info['id']}.{info['ext']}"

_probe=lambda p,cmd:float(subprocess.run(cmd, capture_output=True).stdout or 0)
def valid(p):
    dur=_probe(p,["ffprobe","-v","0","-show_entries","format=duration","-of","csv=p=0",p])
    aud=_probe(p,["ffprobe","-v","0","-select_streams","a","-show_entries",
                  "stream=codec_type","-of","csv=p=0",p])>0
    return aud and MIN_DURATION<=dur<=MAX_DURATION

def portrait(src,dst):
    w,h=RES
    subprocess.run(["ffmpeg","-y","-i",src,"-vf",f"scale={w}:-2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                    "-c:v","libx264","-c:a","copy",dst],check=True)

def drive():
    creds=service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT"]),
        scopes=["https://www.googleapis.com/auth/drive.file"])
    return build("drive","v3",credentials=creds)

def gfolder(d,name):
    q=f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r=d.files().list(q=q,fields="files(id)").execute().get("files",[])
    return r[0]["id"] if r else d.files().create(body={"name":name,
        "mimeType":"application/vnd.google-apps.folder"}).execute()["id"]

def upload(d,fid,p):
    d.files().create(body={"name":p.name,"parents":[fid]},
        media_body=MediaFileUpload(p,"video/mp4")).execute()

def main():
    if {"OPENROUTER_API_KEY","GDRIVE_SERVICE_ACCOUNT"}-os.environ.keys():
        sys.exit("Missing env vars")
    key=os.environ["OPENROUTER_API_KEY"]; vids=fetch()
    ranked=sorted(((u,t,score(t,key)) for u,t in vids), key=lambda x:x[2], reverse=True)
    svc=drive(); fid=gfolder(svc,DRIVE_FOLDER)
    wd=Path(tempfile.mkdtemp()); (wd/"d").mkdir(); (wd/"p").mkdir()

    done=0
    for url,txt,_ in ranked:
        if done>=MAX_TO_UPLOAD: break
        try:
            raw=dl(url,wd/"d")
            if not valid(raw): raw.unlink(); continue
            out=wd/"p"/(raw.stem+".mp4"); portrait(raw,out); raw.unlink()
            upload(svc,fid,out); out.unlink(); done+=1
        except Exception as e: print("skip",url,e,file=sys.stderr)
    print("Uploaded",done,"videos")

if __name__=="__main__": main()
