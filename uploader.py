import gc
import json
import os
from pathlib import Path
import random
import re
import threading
import time

# Enable ANSI color codes on Windows CMD/PowerShell as well as Linux/Docker
if os.name == "nt":
    os.system("")

RED = "\033[91m"
BOLD_RED = "\033[1;91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def log_info(msg: str):
    print(msg, flush=True)


def log_ok(msg: str):
    print(f"{GREEN}{msg}{RESET}", flush=True)


def log_warn(msg: str):
    print(f"{YELLOW}{msg}{RESET}", flush=True)


def log_error(msg: str):
    print(f"{BOLD_RED}{msg}{RESET}", flush=True)


# Automatically resolve paths whether running inside Docker (/app/data) or natively on Windows
IN_DOCKER = os.path.exists("/app/data")
BASE_DATA_DIR = (
    Path("/app/data")
    if IN_DOCKER
    else Path(__file__).resolve().parent / "data"
)

CLIPS_DIR = BASE_DATA_DIR / "clips"
UPLOAD_DIR = BASE_DATA_DIR / "upload"
AUTH_DIR = BASE_DATA_DIR / "auth"

for d in (CLIPS_DIR, UPLOAD_DIR, AUTH_DIR):
    d.mkdir(parents=True, exist_ok=True)

ENABLE_UPLOAD = os.getenv("ENABLE_UPLOAD", "false").lower() == "true"
UPLOAD_TIKTOK = os.getenv("UPLOAD_TIKTOK", "true").lower() == "true"
UPLOAD_YOUTUBE = os.getenv("UPLOAD_YOUTUBE", "true").lower() == "true"
UPLOAD_INSTAGRAM = os.getenv("UPLOAD_INSTAGRAM", "true").lower() == "true"
YT_PRIVACY = os.getenv("YT_PRIVACY", "public").strip().lower()

UPLOAD_MIN_MINUTES = float(os.getenv("UPLOAD_MIN_MINUTES", "25"))
UPLOAD_MAX_MINUTES = float(os.getenv("UPLOAD_MAX_MINUTES", "35"))

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()

HASHTAG_RULES = [
    (
        r"\b(?:gta\s*(?:6|six|vi)|gtavi|vice\s*city|leonida|rockstar)\b",
        ["#gta6", "#gtavi", "#rockstargames", "#vicecity", "#gaming"],
    ),
    (
        r"\b(?:pentakill|quadra|triple|1v\d|league|velja|baron|dragon|challenger)\b",
        ["#leagueoflegends", "#lol", "#lolclips", "#outplay", "#gaming"],
    ),
    (
        r"\b(?:cooked|fail|int|died|throw|no\s+way|icant|kekw)\b",
        ["#gamingfails", "#funny", "#twitchfails", "#viral"],
    ),
]


def load_clip_context(mp4_path: Path) -> dict:
    """Loads metadata from data/clips/<stem>.json, falling back to filename parsing."""
    json_path = CLIPS_DIR / f"{mp4_path.stem}.json"
    meta = {}
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            log_warn(f"[UPLOAD] Could not read {json_path.name}: {e}")

    channel = meta.get("channel", "").strip()
    if not channel:
        m = re.match(r"^(.+?)_\d{8}_\d{6}_score\d+$", mp4_path.stem)
        channel = m.group(1) if m else mp4_path.stem.split("_")[0]

    raw_title = meta.get("generated_title", "").strip().strip('"')
    if not raw_title:
        raw_title = f"{channel} insane stream moment!"

    transcript = meta.get("full_transcript", "").strip()
    saved_tags = meta.get("hashtags", [])

    return {
        "channel": channel,
        "title": raw_title,
        "transcript": transcript,
        "saved_tags": saved_tags,
    }


def build_social_metadata(ctx: dict) -> dict:
    """Builds platform captions with 'Twitch: {creator}' credit and targeted hashtags."""
    channel = ctx["channel"]
    title = ctx["title"]
    transcript = ctx["transcript"]

    clean_creator_tag = re.sub(r"[^a-zA-Z0-9]", "", channel).lower()
    combined = f"{title} {transcript}".lower()

    tags = [f"#{clean_creator_tag}", "#twitch", "#twitchclips", "#gamingclips"]
    if ctx["saved_tags"]:
        tags.extend(ctx["saved_tags"])

    for pattern, topic_tags in HASHTAG_RULES:
        if re.search(pattern, combined):
            tags.extend(topic_tags)
            break
    else:
        tags.extend(["#streamhighlights", "#fyp", "#viral"])

    seen = set()
    unique_tags = []
    for t in tags:
        t_low = t.lower()
        if t_low not in seen:
            seen.add(t_low)
            unique_tags.append(t_low)
    hashtag_str = " ".join(unique_tags[:8])

    yt_title = f"{title} | Twitch: {channel} #Shorts"
    if len(yt_title) > 100:
        yt_title = f"{title} #Shorts"[:100]

    yt_desc = (
        f"{title}\n\n"
        f"Twitch: {channel} (https://twitch.tv/{channel})\n\n"
        f"{hashtag_str} #Shorts"
    )[:4900]

    social_caption = (
        f"{title} 🔥\n\n"
        f"Twitch: {channel}\n"
        f".\n"
        f"{hashtag_str}"
    )[:2100]

    return {
        "yt_title": yt_title,
        "yt_desc": yt_desc,
        "social_caption": social_caption,
        "hashtags": unique_tags[:8],
    }


def upload_to_tiktok(video_path: Path, caption: str) -> bool:
    cookies_path = AUTH_DIR / "tiktok_cookies.txt"
    if not cookies_path.exists():
        log_error(f"[UPLOAD][TIKTOK] FAILED: Missing credentials file {cookies_path}")
        return False

    from tiktok_uploader.upload import upload_video

    upload_video(
        filename=str(video_path),
        description=caption,
        cookies=str(cookies_path),
        headless=True,
    )
    log_ok(f"[UPLOAD][TIKTOK] Published {video_path.name}")
    return True


def upload_to_instagram(video_path: Path, caption: str) -> bool:
    session_path = AUTH_DIR / "ig_session.json"
    if not session_path.exists() and (not IG_USERNAME or not IG_PASSWORD):
        log_error("[UPLOAD][IG] FAILED: Missing ig_session.json and IG_USERNAME/IG_PASSWORD.")
        return False

    from instagrapi import Client

    cl = Client()
    if session_path.exists():
        cl.load_settings(session_path)
    if IG_USERNAME and IG_PASSWORD:
        cl.login(IG_USERNAME, IG_PASSWORD)
        cl.dump_settings(session_path)

    cl.clip_upload(video_path, caption=caption)
    log_ok(f"[UPLOAD][IG] Published Reel {video_path.name}")
    return True


def upload_to_youtube(video_path: Path, title: str, description: str) -> bool:
    token_path = AUTH_DIR / "youtube_token.json"
    if not token_path.exists():
        log_error(f"[UPLOAD][YT] FAILED: Missing credentials file {token_path}")
        return False

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(
        str(token_path), ["https://www.googleapis.com/auth/youtube.upload"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "20",  # Gaming
        },
        "status": {
            "privacyStatus": YT_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    response = request.execute()
    log_ok(f"[UPLOAD][YT] Published Short https://youtu.be/{response.get('id')}")
    return True


def is_file_ready(path: Path) -> bool:
    """Ensures the file is finished moving/copying before uploading."""
    try:
        size1 = path.stat().st_size
        if size1 == 0:
            return False
        time.sleep(1.0)
        return path.exists() and path.stat().st_size == size1
    except OSError:
        return False


class UploadManager:
    """Polls data/upload for approved .mp4 clips (including files already present at startup),
    publishes one every 25-35 minutes across enabled platforms, and deletes the .mp4 after upload.
    """

    def __init__(self):
        self.next_upload_ts = 0.0  # 0.0 ensures any clip already in upload/ starts immediately
        self.last_logged_wait = 0.0
        if ENABLE_UPLOAD:
            threading.Thread(target=self._watch_upload_folder, daemon=True).start()
            log_info(
                f"[UPLOAD] Watching {UPLOAD_DIR} "
                f"(random interval: {UPLOAD_MIN_MINUTES:.0f}-{UPLOAD_MAX_MINUTES:.0f} mins)."
            )
        else:
            log_info("[UPLOAD] Auto-upload disabled (ENABLE_UPLOAD=false).")

    def _schedule_next_window(self) -> float:
        delay_sec = random.uniform(UPLOAD_MIN_MINUTES * 60.0, UPLOAD_MAX_MINUTES * 60.0)
        self.next_upload_ts = time.time() + delay_sec
        return delay_sec

    def _watch_upload_folder(self):
        # Assume clips may already be waiting in data/upload on startup
        startup_pending = sorted(UPLOAD_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if startup_pending:
            log_info(
                f"[UPLOAD] Startup scan found {len(startup_pending)} pending video(s) in "
                f"{UPLOAD_DIR} — starting first upload immediately."
            )
        else:
            log_info(f"[UPLOAD] Startup scan complete: {UPLOAD_DIR} is ready for incoming clips.")

        while True:
            try:
                pending = sorted(
                    UPLOAD_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime
                )
                if not pending:
                    time.sleep(5.0)
                    continue

                video_path = pending[0]
                if not is_file_ready(video_path):
                    time.sleep(2.0)
                    continue

                now = time.time()
                wait_needed = self.next_upload_ts - now
                if wait_needed > 0:
                    if (now - self.last_logged_wait) >= 300:
                        mins, secs = divmod(int(wait_needed), 60)
                        log_info(f"[UPLOAD] Next clip '{video_path.name}' waiting {mins}m {secs}s...")
                        self.last_logged_wait = now
                    time.sleep(min(wait_needed, 10.0))
                    continue

                ctx = load_clip_context(video_path)
                meta = build_social_metadata(ctx)

                log_info(
                    f"[UPLOAD] Publishing '{video_path.name}' | "
                    f"Creator: {ctx['channel']} | Title: \"{ctx['title']}\""
                )

                attempted = 0
                success_count = 0

                if UPLOAD_TIKTOK:
                    attempted += 1
                    try:
                        if upload_to_tiktok(video_path, meta["social_caption"]):
                            success_count += 1
                    except Exception as e:
                        log_error(f"[UPLOAD][TIKTOK] UPLOAD FAILED for '{video_path.name}': {e}")

                if UPLOAD_YOUTUBE:
                    attempted += 1
                    try:
                        if upload_to_youtube(video_path, meta["yt_title"], meta["yt_desc"]):
                            success_count += 1
                    except Exception as e:
                        log_error(f"[UPLOAD][YT] UPLOAD FAILED for '{video_path.name}': {e}")

                if UPLOAD_INSTAGRAM:
                    attempted += 1
                    try:
                        if upload_to_instagram(video_path, meta["social_caption"]):
                            success_count += 1
                    except Exception as e:
                        log_error(f"[UPLOAD][IG] UPLOAD FAILED for '{video_path.name}': {e}")

                gc.collect()

                if attempted == 0:
                    log_error("[UPLOAD] ERROR: All upload platforms are disabled in environment variables!")
                elif success_count > 0:
                    if success_count < attempted:
                        log_error(
                            f"[UPLOAD] PARTIAL FAILURE: Only {success_count}/{attempted} platforms "
                            f"succeeded for '{video_path.name}'."
                        )
                    try:
                        video_path.unlink(missing_ok=True)
                        log_ok(
                            f"[UPLOAD] Uploaded to {success_count}/{attempted} platform(s) -> "
                            f"Deleted '{video_path.name}' from upload folder."
                        )
                    except OSError as e:
                        log_error(f"[UPLOAD] Failed to delete '{video_path.name}': {e}")
                else:
                    log_error(
                        f"[UPLOAD] ALL UPLOADS FAILED (0/{attempted}) for '{video_path.name}' — "
                        "keeping file in upload folder to retry next cycle."
                    )

                delay_sec = self._schedule_next_window()
                self.last_logged_wait = time.time()
                mins, secs = divmod(int(delay_sec), 60)
                log_info(f"[UPLOAD] Cooldown set: next upload eligible in {mins}m {secs}s.")

            except Exception as e:
                log_error(f"[UPLOAD] Watcher error: {e}")
                time.sleep(5.0)


if __name__ == "__main__":
    os.environ["ENABLE_UPLOAD"] = "true"
    ENABLE_UPLOAD = True
    UploadManager()
    while True:
        time.sleep(60)