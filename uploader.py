import os
import queue
import re
import threading
import time

AUTH_DIR = "/app/data/auth"
os.makedirs(AUTH_DIR, exist_ok=True)

ENABLE_UPLOAD = os.getenv("ENABLE_UPLOAD", "false").lower() == "true"
UPLOAD_TIKTOK = os.getenv("UPLOAD_TIKTOK", "true").lower() == "true"
UPLOAD_YOUTUBE = os.getenv("UPLOAD_YOUTUBE", "true").lower() == "true"
UPLOAD_INSTAGRAM = os.getenv("UPLOAD_INSTAGRAM", "true").lower() == "true"
MIN_UPLOAD_INTERVAL_SEC = int(os.getenv("MIN_UPLOAD_INTERVAL_SEC", "1800"))

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()

HASHTAG_RULES = [
    (
        r"gta\s*(?:6|six|vi)|rockstar|lucia|jason|vice\s*city|leonida",
        ["#gta6", "#gtavi", "#rockstargames", "#vicecity", "#gaming"],
    ),
    (
        r"pentakill|quadra|triple|1v\d|league|velja|baron|dragon",
        ["#leagueoflegends", "#lol", "#lolclips", "#gaming", "#outplay"],
    ),
    (
        r"cooked|fail|int|died|throw|no\s+way",
        ["#gamingfails", "#funny", "#twitchfails", "#viral"],
    ),
]


def build_social_metadata(channel, title, transcript):
  """Generates platform-specific titles, descriptions, and targeted hashtags."""
  clean_channel = re.sub(r"[^a-zA-Z0-9]", "", channel).lower()
  combined = f"{title} {transcript}".lower()

  tags = [f"#{clean_channel}", "#twitch", "#twitchclips", "#gamingclips"]
  for pattern, topic_tags in HASHTAG_RULES:
    if re.search(pattern, combined):
      tags.extend(topic_tags)
      break
  else:
    tags.extend(["#streamhighlights", "#fyp", "#viral"])

  # Deduplicate while preserving order, max 8 hashtags
  seen = set()
  unique_tags = []
  for t in tags:
    if t not in seen:
      seen.add(t)
      unique_tags.append(t)
  hashtag_str = " ".join(unique_tags[:8])

  # YouTube Shorts requires <= 100 chars and #Shorts
  yt_title = f"{title} #Shorts"[:100]
  yt_desc = (
      f"{title}\n\nCredit: twitch.tv/{channel}\n\n{hashtag_str} #Shorts"
  )[:4900]

  # TikTok & Instagram Reels caption
  social_caption = f"{title} 🔥\n\n🎥 twitch.tv/{channel}\n.\n{hashtag_str}"[
      :2100
  ]

  return {
      "yt_title": yt_title,
      "yt_desc": yt_desc,
      "social_caption": social_caption,
      "hashtags": unique_tags[:8],
  }


def upload_to_tiktok(video_path, caption):
  cookies_path = os.path.join(AUTH_DIR, "tiktok_cookies.txt")
  if not os.path.exists(cookies_path):
    print("[UPLOAD][TIKTOK] Skipped: /app/data/auth/tiktok_cookies.txt not found.")
    return False

  from tiktok_uploader.upload import upload_video

  upload_video(
      filename=video_path,
      description=caption,
      cookies=cookies_path,
      headless=True,
  )
  print(f"[UPLOAD][TIKTOK] Published {os.path.basename(video_path)}")
  return True


def upload_to_instagram(video_path, caption):
  session_path = os.path.join(AUTH_DIR, "ig_session.json")
  if not os.path.exists(session_path) and (not IG_USERNAME or not IG_PASSWORD):
    print(
        "[UPLOAD][IG] Skipped: Provide ig_session.json or IG_USERNAME/IG_PASSWORD."
    )
    return False

  from instagrapi import Client

  cl = Client()
  if os.path.exists(session_path):
    cl.load_settings(session_path)
  if IG_USERNAME and IG_PASSWORD:
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(session_path)

  cl.clip_upload(video_path, caption=caption)
  print(f"[UPLOAD][IG] Published Reel {os.path.basename(video_path)}")
  return True


def upload_to_youtube(video_path, title, description):
  token_path = os.path.join(AUTH_DIR, "youtube_token.json")
  if not os.path.exists(token_path):
    print("[UPLOAD][YT] Skipped: /app/data/auth/youtube_token.json not found.")
    return False

  from google.auth.transport.requests import Request
  from google.oauth2.credentials import Credentials
  from googleapiclient.discovery import build
  from googleapiclient.http import MediaFileUpload

  creds = Credentials.from_authorized_user_file(
      token_path, ["https://www.googleapis.com/auth/youtube.upload"]
  )
  if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    with open(token_path, "w", encoding="utf-8") as f:
      f.write(creds.to_json())

  youtube = build("youtube", "v3", credentials=creds)
  body = {
      "snippet": {
          "title": title,
          "description": description,
          "categoryId": "20",  # Gaming
      },
      "status": {
          "privacyStatus": "public",
          "selfDeclaredMadeForKids": False,
      },
  }
  media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
  request = youtube.videos().insert(
      part="snippet,status", body=body, media_body=media
  )
  response = request.execute()
  print(
      f"[UPLOAD][YT] Published Short https://youtu.be/{response.get('id')}"
  )
  return True


class UploadManager:
  """Background upload worker that paces uploads across TikTok, YT Shorts, and IG Reels."""

  def __init__(self):
    self.q = queue.Queue()
    self.last_upload_ts = 0.0
    if ENABLE_UPLOAD:
      threading.Thread(target=self._worker, daemon=True).start()
      print(
          f"[UPLOAD] Auto-upload enabled (interval={MIN_UPLOAD_INTERVAL_SEC}s)."
      )
    else:
      print("[UPLOAD] Auto-upload disabled (ENABLE_UPLOAD=false).")

  def enqueue(self, video_path, social_meta):
    if ENABLE_UPLOAD:
      self.q.put((video_path, social_meta))
      print(
          f"[UPLOAD] Queued {os.path.basename(video_path)} (queue size={self.q.qsize()})"
      )

  def _worker(self):
    while True:
      video_path, meta = self.q.get()
      try:
        wait_needed = MIN_UPLOAD_INTERVAL_SEC - (
            time.time() - self.last_upload_ts
        )
        if wait_needed > 0 and self.last_upload_ts > 0:
          print(
              f"[UPLOAD] Pacing uploads: waiting {int(wait_needed)}s before next post..."
          )
          time.sleep(wait_needed)

        if not os.path.exists(video_path):
          continue

        if UPLOAD_TIKTOK:
          try:
            upload_to_tiktok(video_path, meta["social_caption"])
          except Exception as e:
            print(f"[UPLOAD][TIKTOK] Error: {e}")

        if UPLOAD_YOUTUBE:
          try:
            upload_to_youtube(video_path, meta["yt_title"], meta["yt_desc"])
          except Exception as e:
            print(f"[UPLOAD][YT] Error: {e}")

        if UPLOAD_INSTAGRAM:
          try:
            upload_to_instagram(video_path, meta["social_caption"])
          except Exception as e:
            print(f"[UPLOAD][IG] Error: {e}")

        self.last_upload_ts = time.time()
      finally:
        self.q.task_done()