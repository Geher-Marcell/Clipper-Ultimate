import gc
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse

if os.name == "nt":
    os.system("")

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


IN_DOCKER = os.path.exists("/app/data")
BASE_DATA_DIR = (
    Path("/app/data")
    if IN_DOCKER
    else Path(__file__).resolve().parent / "data"
)

CLIPS_DIR = BASE_DATA_DIR / "clips"
UPLOAD_DIR = BASE_DATA_DIR / "upload"
STATE_DIR = UPLOAD_DIR / ".state"
AUTH_DIR = BASE_DATA_DIR / "auth"

for d in (CLIPS_DIR, UPLOAD_DIR, STATE_DIR, AUTH_DIR):
    d.mkdir(parents=True, exist_ok=True)

ENABLE_UPLOAD = os.getenv("ENABLE_UPLOAD", "false").lower() == "true"
AUTO_ENQUEUE = os.getenv("AUTO_ENQUEUE", "false").lower() == "true"
UPLOAD_TIKTOK = os.getenv("UPLOAD_TIKTOK", "true").lower() == "true"
UPLOAD_YOUTUBE = os.getenv("UPLOAD_YOUTUBE", "true").lower() == "true"
UPLOAD_INSTAGRAM = os.getenv("UPLOAD_INSTAGRAM", "true").lower() == "true"
YT_PRIVACY = os.getenv("YT_PRIVACY", "public").strip().lower()

UPLOAD_MIN_MINUTES = float(os.getenv("UPLOAD_MIN_MINUTES", "25"))
UPLOAD_MAX_MINUTES = float(os.getenv("UPLOAD_MAX_MINUTES", "35"))

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip()

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

STEALTH_AND_MODAL_JS = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    if (navigator.userAgentData) {
      Object.defineProperty(navigator, 'userAgentData', {
        get: () => ({
          brands: [
            { brand: 'Google Chrome', version: '131' },
            { brand: 'Chromium', version: '131' },
            { brand: 'Not_A Brand', version: '24' }
          ],
          mobile: false,
          platform: 'Windows'
        })
      });
    }
    window.chrome = window.chrome || { runtime: {}, loadTimes: () => {}, csi: () => {} };
  } catch (e) {}

  const cleanModals = () => {
    if (window.__allowPostModal) return;
    try {
      const buttons = document.querySelectorAll(
        '[data-floating-ui-portal] button, .TUXModal-overlay button, .common-modal-body button'
      );
      for (const btn of buttons) {
        const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
        if (['got it', 'not now', 'cancel', 'close', 'dismiss', 'ok'].includes(txt)) {
          btn.click();
        }
      }
      document.querySelectorAll('.TUXModal-overlay, [data-floating-ui-portal]').forEach(el => {
        if (
          el.classList.contains('TUXModal-overlay') ||
          el.querySelector('.TUXModal-overlay, .common-modal-body')
        ) {
          el.remove();
        }
      });
    } catch (e) {}
  };

  const start = () => {
    cleanModals();
    new MutationObserver(cleanModals).observe(document.documentElement, { childList: true, subtree: true });
    setInterval(cleanModals, 500);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
"""


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


def build_social_metadata(
    ctx, title: str = "", transcript: str = "", saved_tags=None
) -> dict:
    """Builds platform captions with 'Twitch: {creator}' credit and targeted hashtags."""
    if isinstance(ctx, dict):
        channel = str(ctx.get("channel", ""))
        title = str(ctx.get("title", ""))
        transcript = str(ctx.get("transcript", ""))
        saved_tags = ctx.get("saved_tags") or []
    else:
        channel = str(ctx)
        saved_tags = saved_tags or []

    clean_creator_tag = re.sub(r"[^a-zA-Z0-9]", "", channel).lower()
    combined = f"{title} {transcript}".lower()

    tags = [f"#{clean_creator_tag}", "#twitch", "#twitchclips", "#gamingclips"]
    if saved_tags:
        tags.extend(saved_tags)

    for pattern, topic_tags in HASHTAG_RULES:
        if re.search(pattern, combined):
            tags.extend(topic_tags)
            break
    else:
        tags.extend(["#streamhighlights", "#fyp", "#viral"])

    seen = set()
    unique_tags = []
    for t in tags:
        t_low = str(t).lower().strip()
        if t_low and t_low not in seen:
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


def extract_thumbnail(video_path: Path):
    """Extracts a clean JPEG cover frame via ffmpeg so instagrapi doesn't rely on moviepy."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        try:
            import imageio_ffmpeg
            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    thumb_path = Path(tempfile.gettempdir()) / f"{video_path.stem}_ig_cover.jpg"
    try:
        subprocess.run(
            [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.5", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2",
                str(thumb_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            return thumb_path
    except Exception:
        pass
    return None


def load_netscape_cookies(cookies_path: Path) -> list:
    """Parses Netscape cookies.txt into clean, deduplicated Playwright cookie dicts."""
    now_ts = int(time.time())
    deduped = {}

    for line in cookies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        domain = domain.strip()
        path = path.strip() or "/"
        name = name.strip()
        if not name or not domain:
            continue

        exp_val = None
        try:
            parsed_exp = int(float(expires))
            if parsed_exp > 0:
                if parsed_exp < now_ts:
                    continue
                exp_val = parsed_exp
        except ValueError:
            pass

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": True,
            "sameSite": "None",
        }
        if exp_val is not None:
            cookie["expires"] = exp_val

        deduped[(name, domain, path)] = cookie

    return list(deduped.values())


def _is_bad_gateway_page(page, response) -> bool:
    status = response.status if response else 0
    if status >= 500:
        return True
    try:
        title = (page.title() or "").lower()
        if "502" in title or "bad gateway" in title or "503" in title or "504" in title:
            return True
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
        if "502 bad gateway" in body or "504 gateway" in body:
            return True
    except Exception:
        pass
    return False


def _all_frames(page):
    return [page.main_frame] + [f for f in page.frames if f != page.main_frame]


def accept_tiktok_cookie_banner(page):
    """Clicks 'Allow all' on TikTok's GDPR cookie banner across all frames and shadow roots."""
    for frame in _all_frames(page):
        try:
            allow_btn = frame.locator("button:has-text('Allow all')").first
            if allow_btn.count() > 0 and allow_btn.is_visible():
                allow_btn.click(timeout=3000, force=True)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

        try:
            clicked = frame.evaluate(
                """() => {
                    const clickAllow = (root) => {
                        for (const btn of root.querySelectorAll('button')) {
                            const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                            if (txt === 'allow all') {
                                btn.click();
                                return true;
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot && clickAllow(el.shadowRoot)) return true;
                        }
                        return false;
                    };
                    return clickAllow(document);
                }"""
            )
            if clicked:
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass


def _select_video_still_visible(page) -> bool:
    for frame in _all_frames(page):
        try:
            btn = frame.locator("button:has-text('Select video')").first
            if btn.count() > 0 and btn.is_visible():
                return True
        except Exception:
            pass
    return False


def attach_video_to_tiktok(page, video_path: Path) -> str:
    """Attaches the video across all frames via video file input or native file chooser."""
    for selector in ("input[type='file'][accept*='video']", "input[type='file']"):
        for frame in _all_frames(page):
            try:
                loc = frame.locator(selector)
                if loc.count() > 0:
                    loc.first.set_input_files(str(video_path))
                    page.wait_for_timeout(3000)
                    if not _select_video_still_visible(page):
                        return f"direct input ({selector})"
            except Exception:
                continue

    for frame in _all_frames(page):
        try:
            select_btn = frame.locator("button:has-text('Select video')").first
            if select_btn.count() > 0 and select_btn.is_visible():
                with page.expect_file_chooser(timeout=12000) as fc_info:
                    select_btn.click(force=True)
                fc_info.value.set_files(str(video_path))
                return "file chooser (Select video button)"
        except Exception:
            continue

    raise RuntimeError("No usable video file input or 'Select video' button found in any frame.")


def upload_to_tiktok(video_path: Path, caption: str) -> bool:
    cookies_path = AUTH_DIR / "tiktok_cookies.txt"
    if not cookies_path.exists():
        log_error(f"[UPLOAD][TIKTOK] FAILED: Missing credentials file {cookies_path}")
        return False

    cookies = load_netscape_cookies(cookies_path)
    if not cookies:
        log_error(f"[UPLOAD][TIKTOK] FAILED: No valid cookies parsed from {cookies_path}")
        return False

    from playwright.sync_api import sync_playwright

    debug_screenshot = UPLOAD_DIR / "tiktok_debug.png"
    debug_screenshot.unlink(missing_ok=True)

    chrome_bin = "/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None
    launch_kwargs = {
        "headless": True,
        "args": [
            "--headless=new",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--enable-webgl",
        ],
    }
    if chrome_bin:
        launch_kwargs["executable_path"] = chrome_bin

    upload_urls = [
        "https://www.tiktok.com/tiktokstudio/upload",
        "https://www.tiktok.com/creator-center/upload",
        "https://www.tiktok.com/upload?lang=en",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(60000)
        context.set_default_navigation_timeout(90000)
        context.add_init_script(STEALTH_AND_MODAL_JS)
        context.add_cookies(cookies)

        page = context.new_page()
        publish_confirmed = {"ok": False}

        def handle_response(response):
            try:
                url_low = response.url.lower()
                if any(
                    k in url_low
                    for k in ("/item/create", "/create/aweme", "/project/post", "/post/item", "/publish")
                ):
                    if response.status == 200:
                        compact = response.text().replace(" ", "").lower()
                        if '"status_code":0' in compact or '"statuscode":0' in compact:
                            publish_confirmed["ok"] = True
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            try:
                page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                accept_tiktok_cookie_banner(page)
            except Exception:
                pass

            loaded_upload_page = False
            for attempt in range(6):
                target_url = upload_urls[attempt % len(upload_urls)]
                resp = page.goto(target_url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(4500)

                if _is_bad_gateway_page(page, resp):
                    status = resp.status if resp else 502
                    log_warn(
                        f"[UPLOAD][TIKTOK] HTTP {status} (Bad Gateway) on {target_url} — "
                        f"retrying next endpoint ({attempt + 1}/6)..."
                    )
                    page.wait_for_timeout(4000)
                    continue

                loaded_upload_page = True
                break

            if not loaded_upload_page:
                page.screenshot(path=str(debug_screenshot))
                log_error(
                    "[UPLOAD][TIKTOK] FAILED: TikTok returned 502 Bad Gateway across all upload endpoints."
                )
                return False

            if "/login" in page.url.lower():
                page.screenshot(path=str(debug_screenshot))
                log_error(
                    "[UPLOAD][TIKTOK] FAILED: Redirected to login page — "
                    "refresh data/auth/tiktok_cookies.txt using save_tiktok_cookies.py."
                )
                return False

            accept_tiktok_cookie_banner(page)
            method = attach_video_to_tiktok(page, video_path)
            log_info(f"[UPLOAD][TIKTOK] Attached '{video_path.name}' via {method}, waiting for editor...")

            def find_visible_editor():
                selectors = [
                    "[data-lexical-editor='true']",
                    ".public-DraftEditor-content",
                    "div[contenteditable='true']",
                    "div[contenteditable='plaintext-only']",
                ]
                for frame in _all_frames(page):
                    for sel in selectors:
                        try:
                            loc = frame.locator(sel)
                            for i in range(loc.count()):
                                item = loc.nth(i)
                                if not item.is_visible():
                                    continue
                                box = item.bounding_box()
                                if box and box["width"] >= 120 and box["height"] >= 20:
                                    return item, box
                        except Exception:
                            continue
                return None, None

            retries_used = 0
            active_editor = None
            editor_box = None

            for _ in range(45):
                active_editor, editor_box = find_visible_editor()
                if active_editor and editor_box:
                    break

                for frame in _all_frames(page):
                    try:
                        retry_btn = frame.locator("button:has-text('Retry')").first
                        if retry_btn.count() > 0 and retry_btn.is_visible():
                            if retries_used < 3:
                                retries_used += 1
                                log_warn(
                                    f"[UPLOAD][TIKTOK] TikTok Studio showed 'Something went wrong' — "
                                    f"clicking Retry ({retries_used}/3)..."
                                )
                                page.wait_for_timeout(2500)
                                retry_btn.click(force=True)
                                page.wait_for_timeout(3000)
                                attach_video_to_tiktok(page, video_path)
                            break
                    except Exception:
                        continue

                page.wait_for_timeout(1500)

            if not active_editor or not editor_box:
                raise RuntimeError("Could not find a visible caption editor box in any frame on TikTok Studio.")

            page.wait_for_timeout(1500)
            page.mouse.click(
                editor_box["x"] + (editor_box["width"] / 2.0),
                editor_box["y"] + (editor_box["height"] / 2.0),
            )
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(300)

            lines = caption.splitlines()
            for idx, line in enumerate(lines):
                if line:
                    page.keyboard.insert_text(line)
                if idx < len(lines) - 1:
                    page.keyboard.press("Shift+Enter")

            page.wait_for_timeout(800)
            page.keyboard.press("Escape")

            def find_ready_post_button():
                for frame in _all_frames(page):
                    try:
                        btn = frame.locator("button[data-e2e='post_video_button']").first
                        if btn.count() > 0 and btn.is_visible():
                            disabled_attr = btn.get_attribute("disabled")
                            aria_disabled = (btn.get_attribute("aria-disabled") or "").lower()
                            cls = (btn.get_attribute("class") or "").lower()
                            if disabled_attr is None and aria_disabled != "true" and "disabled" not in cls:
                                return btn

                        candidates = frame.locator("button").filter(has_text=re.compile(r"^\s*Post\s*$"))
                        for i in range(candidates.count()):
                            cand = candidates.nth(i)
                            if not cand.is_visible():
                                continue
                            disabled_attr = cand.get_attribute("disabled")
                            aria_disabled = (cand.get_attribute("aria-disabled") or "").lower()
                            cls = (cand.get_attribute("class") or "").lower()
                            if disabled_attr is None and aria_disabled != "true" and "disabled" not in cls:
                                return cand
                    except Exception:
                        continue
                return None

            post_btn = None
            for _ in range(90):
                post_btn = find_ready_post_button()
                if post_btn:
                    break
                page.wait_for_timeout(1000)

            if not post_btn:
                raise RuntimeError("Post button never became enabled (video upload or copyright check stalled).")

            for frame in _all_frames(page):
                try:
                    frame.evaluate("window.__allowPostModal = true;")
                except Exception:
                    pass

            post_btn.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            try:
                post_btn.click(timeout=10000)
            except Exception:
                post_btn.click(force=True)

            is_published = False
            for _ in range(45):
                if publish_confirmed["ok"]:
                    is_published = True
                    break

                url_low = page.url.lower()
                if "/content" in url_low or "/manage" in url_low:
                    is_published = True
                    break

                for frame in _all_frames(page):
                    try:
                        if frame.locator("text=/your video is being uploaded|video published|manage your posts/i").count() > 0:
                            is_published = True
                            break

                        dialog_btns = frame.locator(
                            "[data-floating-ui-portal] button, .TUXModal-overlay button, div[role='dialog'] button"
                        ).filter(has_text=re.compile(r"^\s*(Post|Post now|Continue)\s*$", re.I))
                        if dialog_btns.count() > 0 and dialog_btns.first.is_visible():
                            try:
                                dialog_btns.first.click(timeout=3000)
                            except Exception:
                                dialog_btns.first.click(force=True)
                    except Exception:
                        continue

                if is_published:
                    break
                page.wait_for_timeout(1000)

            if not is_published:
                page.screenshot(path=str(debug_screenshot))
                log_error(
                    f"[UPLOAD][TIKTOK] FAILED: Clicked Post on '{page.url}', "
                    f"but TikTok did not confirm upload (saved debug screenshot to {debug_screenshot})."
                )
                return False

            debug_screenshot.unlink(missing_ok=True)
            log_ok(f"[UPLOAD][TIKTOK] Published {video_path.name}")
            return True

        except Exception as e:
            try:
                page.screenshot(path=str(debug_screenshot))
                log_error(
                    f"[UPLOAD][TIKTOK] FAILED at URL '{page.url}' "
                    f"(saved debug screenshot to {debug_screenshot}): {e}"
                )
            except Exception:
                log_error(f"[UPLOAD][TIKTOK] FAILED: {e}")
            return False
        finally:
            browser.close()


def upload_to_instagram(video_path: Path, caption: str) -> bool:
    session_path = AUTH_DIR / "ig_session.json"
    sessionid_path = AUTH_DIR / "ig_sessionid.txt"

    raw_sid = IG_SESSIONID
    if not raw_sid and sessionid_path.exists():
        raw_sid = sessionid_path.read_text(encoding="utf-8").strip()

    if not session_path.exists() and not raw_sid and (not IG_USERNAME or not IG_PASSWORD):
        log_error("[UPLOAD][IG] FAILED: Missing ig_sessionid.txt (or ig_session.json).")
        return False

    from instagrapi import Client

    cl = Client()
    cl.delay_range = [1, 3]
    if getattr(cl, "timezone_offset", None) is None:
        cl.timezone_offset = 0

    if session_path.exists():
        cl.load_settings(session_path)
    elif raw_sid:
        sid = urllib.parse.unquote(raw_sid)
        cl.login_by_sessionid(sid)
        cl.dump_settings(session_path)
    elif IG_USERNAME and IG_PASSWORD:
        cl.login(IG_USERNAME, IG_PASSWORD)
        cl.dump_settings(session_path)

    if not cl.user_id:
        log_error("[UPLOAD][IG] FAILED: Instagram session has no user_id.")
        return False

    thumb_path = extract_thumbnail(video_path)
    try:
        if thumb_path:
            cl.clip_upload(video_path, caption=caption, thumbnail=thumb_path)
        else:
            cl.clip_upload(video_path, caption=caption)
        cl.dump_settings(session_path)
        log_ok(f"[UPLOAD][IG] Published Reel {video_path.name}")
        return True
    except TypeError as e:
        last_json = getattr(cl, "last_json", None)
        if (
            "NoneType" in str(e)
            and isinstance(last_json, dict)
            and last_json.get("status") == "ok"
            and "media" in last_json
        ):
            cl.dump_settings(session_path)
            log_ok(
                f"[UPLOAD][IG] Published Reel {video_path.name} "
                "(ignored post-upload response parser NoneType warning)"
            )
            return True
        raise
    finally:
        if thumb_path and thumb_path.exists():
            try:
                thumb_path.unlink()
            except OSError:
                pass


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
            "categoryId": "20",
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
    """Polls data/upload for approved .mp4 clips, publishes one every 25-35 minutes
    across enabled platforms, tracks per-platform success so partial failures retry
    only missing platforms, and deletes the .mp4 once all enabled platforms succeed.
    """

    def __init__(self):
        self.next_upload_ts = 0.0
        self.last_logged_wait = 0.0
        if ENABLE_UPLOAD:
            threading.Thread(target=self._watch_upload_folder, daemon=True).start()
            log_info(
                f"[UPLOAD] Watching {UPLOAD_DIR} "
                f"(random interval: {UPLOAD_MIN_MINUTES:.0f}-{UPLOAD_MAX_MINUTES:.0f} mins)."
            )
        else:
            log_info("[UPLOAD] Auto-upload disabled (ENABLE_UPLOAD=false).")

    def enqueue(self, video_path, social_meta=None):
        """Called by clipper.py after rendering a clip into data/clips/."""
        p = Path(video_path)
        if AUTO_ENQUEUE and p.exists():
            dest = UPLOAD_DIR / p.name
            if not dest.exists():
                shutil.copy2(p, dest)
                log_info(f"[UPLOAD] Auto-enqueued '{p.name}' to {UPLOAD_DIR}.")
        else:
            log_info(
                f"[UPLOAD] Clip '{p.name}' ready in {CLIPS_DIR} — "
                "approve via review_clips.py (or move to data/upload) to publish."
            )

    def _schedule_next_window(self) -> float:
        delay_sec = random.uniform(UPLOAD_MIN_MINUTES * 60.0, UPLOAD_MAX_MINUTES * 60.0)
        self.next_upload_ts = time.time() + delay_sec
        return delay_sec

    def _load_state(self, video_path: Path) -> dict:
        state_file = STATE_DIR / f"{video_path.stem}.json"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self, video_path: Path, state: dict):
        state_file = STATE_DIR / f"{video_path.stem}.json"
        try:
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            pass

    def _clear_state(self, video_path: Path):
        state_file = STATE_DIR / f"{video_path.stem}.json"
        try:
            state_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _watch_upload_folder(self):
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

                enabled_platforms = []
                if UPLOAD_TIKTOK:
                    enabled_platforms.append("tiktok")
                if UPLOAD_YOUTUBE:
                    enabled_platforms.append("youtube")
                if UPLOAD_INSTAGRAM:
                    enabled_platforms.append("instagram")

                if not enabled_platforms:
                    log_error("[UPLOAD] ERROR: All upload platforms are disabled in environment variables!")
                    time.sleep(30.0)
                    continue

                ctx = load_clip_context(video_path)
                meta = build_social_metadata(ctx)
                done = self._load_state(video_path)

                log_info(
                    f"[UPLOAD] Publishing '{video_path.name}' | "
                    f"Creator: {ctx['channel']} | Title: \"{ctx['title']}\""
                )

                if UPLOAD_TIKTOK and not done.get("tiktok"):
                    try:
                        if upload_to_tiktok(video_path, meta["social_caption"]):
                            done["tiktok"] = True
                            self._save_state(video_path, done)
                    except Exception as e:
                        log_error(f"[UPLOAD][TIKTOK] UPLOAD FAILED for '{video_path.name}': {e}")
                        log_error(traceback.format_exc())

                if UPLOAD_YOUTUBE and not done.get("youtube"):
                    try:
                        if upload_to_youtube(video_path, meta["yt_title"], meta["yt_desc"]):
                            done["youtube"] = True
                            self._save_state(video_path, done)
                    except Exception as e:
                        log_error(f"[UPLOAD][YT] UPLOAD FAILED for '{video_path.name}': {e}")
                        log_error(traceback.format_exc())

                if UPLOAD_INSTAGRAM and not done.get("instagram"):
                    try:
                        if upload_to_instagram(video_path, meta["social_caption"]):
                            done["instagram"] = True
                            self._save_state(video_path, done)
                    except Exception as e:
                        log_error(f"[UPLOAD][IG] UPLOAD FAILED for '{video_path.name}': {e}")
                        log_error(traceback.format_exc())

                gc.collect()

                succeeded = [p for p in enabled_platforms if done.get(p)]
                remaining = [p for p in enabled_platforms if not done.get(p)]

                if not remaining:
                    try:
                        video_path.unlink(missing_ok=True)
                        self._clear_state(video_path)
                        log_ok(
                            f"[UPLOAD] Uploaded to {len(succeeded)}/{len(enabled_platforms)} platform(s) -> "
                            f"Deleted '{video_path.name}' from upload folder."
                        )
                    except OSError as e:
                        log_error(f"[UPLOAD] Failed to delete '{video_path.name}': {e}")
                elif succeeded:
                    log_error(
                        f"[UPLOAD] PARTIAL FAILURE ({len(succeeded)}/{len(enabled_platforms)} succeeded) for "
                        f"'{video_path.name}' — keeping file to retry: {', '.join(remaining)}."
                    )
                else:
                    log_error(
                        f"[UPLOAD] ALL UPLOADS FAILED (0/{len(enabled_platforms)}) for '{video_path.name}' — "
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