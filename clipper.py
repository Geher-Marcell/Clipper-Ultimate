from collections import Counter, deque
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import textwrap
import threading
import time
import cv2
from faster_whisper import WhisperModel
from uploader import UploadManager, build_social_metadata

# --- Configuration ---
STREAM_URLS = [
    u.strip()
    for u in os.getenv(
        "STREAM_URLS",
        os.getenv("STREAM_URL", "https://www.twitch.tv/velja_lol"),
    ).split(",")
    if u.strip()
]
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "base.en")
BURN_SUBTITLES = os.getenv("BURN_SUBTITLES", "true").lower() == "true"

INITIAL_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "65"))
MIN_THRESHOLD = 52.0
MAX_THRESHOLD = 88.0
TARGET_CLIPS_PER_HOUR = 3.0

BASE_BUFFER_DIR = "/app/data/buffer"
CLIPS_DIR = "/app/data/clips"
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

SEGMENT_TIME = 4
BUFFER_SEGMENTS = 25
CLIP_PRE_SECONDS = int(os.getenv("CLIP_PRE_SECONDS", "30"))
CLIP_POST_SECONDS = int(os.getenv("CLIP_POST_SECONDS", "0"))
COOLDOWN_SECONDS = 45

CHAT_HYPE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:cooked|cinema|cinematic|ts|pmo|sybau|icl|ong|fr+|nah+|bro+|aura|unc|glaz(?:e|ing)|"
    r"type\s*shit|lock\s*in|peak|motion|valid|joever|icant|aware|pause|ayoo+|holyy+|yooo+|nooo+|"
    r"clipped?|w{3,}|l{3,}|kekw|omegalul|lulw?|pog+|poggers|omg|wtf|insane|crazy)\b"
    r"|"
    r"\b(?:ain'?t\s+no\s+way|no\s+way|hell\s+nah|we\s+are\s+so\s+back|it'?s\s+over|"
    r"chat\s+is\s+this\s+real|absolute\s+cinema|what\s+the\s+helly?)\b"
    r"|"
    r"\b(?:gta\s*(?:6|vi)|gtavi|vice\s*city|leonida|goty)\b"
    r"|"
    r"\?{3,}|!{3,}|7{3,}"
    r")",
    re.IGNORECASE,
)

SPEECH_HYPE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:clip\s+(?:that|it)|absolute\s+cinema|we(?:'re|\s+are)\s+cooked|we\s+are\s+so\s+back|"
    r"ain'?t\s+no\s+way|no\s+way|no\s+shot|chat\s+is\s+this\s+real|what\s+just\s+happened|"
    r"oh\s+my\s+god|let'?s\s+go|are\s+you\s+(?:kidding|serious)|holy\s+shit|bro\s+what|"
    r"what\s+the\s+(?:hell|fuck)|insane|crazy)\b"
    r")",
    re.IGNORECASE,
)

EMOTES_AND_FILLER = {
    "the", "and", "that", "this", "what", "with", "for", "you", "are", "bro",
    "nah", "omg", "wtf", "lol", "lmao", "kekw", "lul", "lulw", "omegalul",
    "pog", "poggers", "icant", "aware", "yes", "not", "can", "just", "like",
    "chat", "stream", "clip", "clipped", "his", "her", "they", "them", "was",
    "were", "have", "had", "out", "get", "got", "how", "why", "who", "when",
    "yeah", "yep", "nope", "wait", "look", "man", "dude", "guys", "actually",
    "literally", "really", "very", "here", "there", "going", "gonna", "wanna",
}

HOOK_BOOST_WORDS = {
    "way", "shot", "what", "how", "why", "cooked", "cinema", "insane", "crazy",
    "dead", "died", "kill", "killed", "clutch", "penta", "pentakill", "quadra",
    "heist", "cops", "police", "wasted", "busted", "holy", "god", "real",
    "happened", "stop", "impossible", "bro", "survived", "missed", "stolen",
}

STOPWORDS = {
    "the",
    "and",
    "that",
    "this",
    "what",
    "with",
    "for",
    "you",
    "are",
    "bro",
    "nah",
    "omg",
    "wtf",
    "lol",
    "lmao",
    "kekw",
    "lul",
    "lulw",
    "omegalul",
    "pog",
    "poggers",
    "icant",
    "aware",
    "yes",
    "not",
    "can",
    "just",
    "like",
}

os.makedirs(BASE_BUFFER_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

print(
    f"[INIT] Loading Faster-Whisper ({WHISPER_MODEL_NAME}) on CUDA for"
    f" {len(STREAM_URLS)} streams..."
)
whisper_model = WhisperModel(
    WHISPER_MODEL_NAME, device="cuda", compute_type="float16"
)
gpu_lock = threading.Lock()
uploader = UploadManager()

_cascade_cls = getattr(cv2, "CascadeClassifier", None)
FACE_CASCADE = (
    _cascade_cls(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if _cascade_cls and hasattr(cv2, "data")
    else None
)


def get_sorted_segments(directory):
  """Safely lists and sorts .ts segments by mtime without crashing on concurrent deletion."""
  if not os.path.exists(directory):
    return []
  items = []
  for fname in os.listdir(directory):
    if not fname.endswith(".ts"):
      continue
    fpath = os.path.join(directory, fname)
    try:
      items.append((os.path.getmtime(fpath), fpath))
    except FileNotFoundError:
      continue
  items.sort(key=lambda x: x[0])
  return [fpath for _, fpath in items]


def format_ass_timestamp(seconds):
  hrs = int(seconds // 3600)
  mins = int((seconds % 3600) // 60)
  secs = int(seconds % 60)
  centis = int(round((seconds - int(seconds)) * 100))
  if centis >= 100:
    secs += 1
    centis = 0
  return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


def escape_ass_text(text):
  return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def transcribe_and_build_punchy_ass(mp4_path, ass_path, has_webcam=False):
  """Transcribes with word timestamps on GPU and builds a kinetic CapCut-style .ass file."""
  words = []
  full_lines = []

  with gpu_lock:
    segments, _ = whisper_model.transcribe(
        mp4_path, beam_size=2, vad_filter=True, word_timestamps=True
    )
    for seg in segments:
      seg_text = seg.text.strip()
      if seg_text:
        full_lines.append(seg_text)
      if seg.words:
        for w in seg.words:
          clean_w = w.word.strip()
          if clean_w:
            words.append({
                "word": clean_w.upper(),
                "start": float(w.start),
                "end": float(w.end),
            })

  chunks, current_chunk = [], []
  for w in words:
    current_chunk.append(w)
    duration = current_chunk[-1]["end"] - current_chunk[0]["start"]
    if (
        len(current_chunk) >= 3
        or duration >= 1.35
        or w["word"].endswith((".", "!", "?"))
    ):
      chunks.append(current_chunk)
      current_chunk = []
  if current_chunk:
    chunks.append(current_chunk)

  margin_v = 630 if has_webcam else 470
  ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Punchy,Liberation Sans,76,&H00FFFFFF,&H0000FFFF,&H00000000,&H99000000,-1,0,0,0,100,100,1.5,0,1,6,3,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

  events = []
  for chunk in chunks:
    for active_idx, active_word in enumerate(chunk):
      w_start = active_word["start"]
      w_end = (
          max(w_start + 0.08, chunk[active_idx + 1]["start"])
          if active_idx + 1 < len(chunk)
          else max(w_start + 0.12, active_word["end"])
      )
      if w_end <= 2.6:
        continue
      w_start = max(w_start, 2.6)

      styled_words = [
          (
              r"{\c&H0000FFFF&\fscx114\fscy114\t(0,90,\fscx104\fscy104)}"
              + escape_ass_text(w["word"])
              + r"{\r}"
              if i == active_idx
              else escape_ass_text(w["word"])
          )
          for i, w in enumerate(chunk)
      ]
      events.append(
          f"Dialogue: 0,{format_ass_timestamp(w_start)},{format_ass_timestamp(w_end)},Punchy,,0,0,0,,{' '.join(styled_words)}"
      )

  with open(ass_path, "w", encoding="utf-8") as f:
    f.write(ass_header + "\n".join(events) + "\n")

  return " ".join(full_lines)


def generate_dynamic_title(channel, transcript, sample_chat_msgs):
    """Dynamically builds a punchy title from the streamer's best spoken quote
    or synthesizes a topic + chat-sentiment hook without any external AI/LLM.
    """
    clean_channel = channel.replace("_", " ").title()
    chat_text = " ".join(sample_chat_msgs)
    chat_low = chat_text.lower()

    # Extract meaningful keywords from chat to weight matching spoken phrases
    chat_tokens = [
        t for t in re.findall(r"\b[a-zA-Z0-9]{3,15}\b", chat_low)
        if t not in EMOTES_AND_FILLER
    ]
    chat_counts = Counter(chat_tokens)

    # --- Strategy 1: Mine the sharpest 3-7 word spoken quote from Whisper ---
    best_quote = ""
    best_score = 0.0

    if transcript:
        # Split on sentence boundaries and strong pauses
        raw_clauses = re.split(r"[.!?\n]+|,\s+(?=(?:what|how|why|no|bro|wait|oh|are|did)\b)", transcript)
        for clause in raw_clauses:
            cleaned = re.sub(r"\s+", " ", clause).strip(" .,!?;:\"'()-")
            words = cleaned.split()
            if len(words) < 3:
                continue

            # Trim overly long sentences down to a crisp 6-word hook so it fits 9:16 intro cleanly
            if len(words) > 7:
                words = words[:6]
                cleaned = " ".join(words)

            if len(cleaned) > 40:
                continue

            score = 1.0
            lower_words = [w.lower().strip(".,!?\"'") for w in words]

            # Boost clauses containing high-emotion trigger words
            score += sum(1.5 for w in lower_words if w in HOOK_BOOST_WORDS)
            # Boost if the streamer said a word that Twitch chat was also spamming
            score += sum(min(3.0, chat_counts[w] * 0.6) for w in lower_words if w in chat_counts)
            # Sweet-spot length bonus (4 to 6 words)
            if 4 <= len(words) <= 6:
                score += 1.5

            if score > best_score:
                best_score = score
                best_quote = cleaned

    if best_quote and best_score >= 3.0:
        # Capitalize first letter cleanly
        formatted_quote = best_quote[0].upper() + best_quote[1:]
        if not formatted_quote.endswith(("!", "?")):
            formatted_quote += "!"
        return f'"{formatted_quote}"'

    # --- Strategy 2: Synthesize Topic + Chat Sentiment when speech is non-verbal ---
    transcript_tokens = [
        t for t in re.findall(r"\b[a-zA-Z0-9]{3,15}\b", transcript.lower())
        if t not in EMOTES_AND_FILLER
    ]
    combined_counts = chat_counts + Counter(transcript_tokens)

    # Detect dominant chat mood
    if re.search(r"\b(?:kekw|omegalul|icant|lmao|lulw?|haha)\b", chat_low):
        mood = "laugh"
    elif re.search(r"\b(?:wtf|no\s+way|aware|cooked|pause|joever)\b|\?{3,}", chat_low):
        mood = "shock"
    else:
        mood = "hype"

    if combined_counts:
        top_topic, freq = combined_counts.most_common(1)[0]
        if freq >= 2:
            topic_display = top_topic.upper() if len(top_topic) <= 4 else top_topic.capitalize()
            if mood == "laugh":
                return f"{clean_channel} has chat dying over {topic_display}"
            if mood == "shock":
                return f"{clean_channel} can't believe that {topic_display} play"
            return f"{clean_channel}'s insane {topic_display} moment"

    # --- Strategy 3: Pure mood fallback if zero keywords were spoken or typed ---
    if mood == "laugh":
        return f"{clean_channel} has chat losing it"
    if mood == "shock":
        return f"{clean_channel} leaves chat speechless"
    return f"Peak cinema on {clean_channel}'s stream"


def detect_webcam_box(mp4_path):
  """Downscales 7 sampled frames to 640x360 for fast Haar Cascade webcam detection."""
  cap = cv2.VideoCapture(mp4_path)
  if not cap.isOpened():
    return None

  total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
  vid_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
  vid_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
  if total_frames < 10 or vid_w == 0 or vid_h == 0:
    cap.release()
    return None

  det_w, det_h = 640, 360
  sx, sy = vid_w / det_w, vid_h / det_h
  detections = []

  for ratio in (0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * ratio))
    ok, frame = cap.read()
    if not ok:
      continue
    small_gray = cv2.cvtColor(
        cv2.resize(frame, (det_w, det_h)), cv2.COLOR_BGR2GRAY
    )
    faces = FACE_CASCADE.detectMultiScale(
        small_gray,
        scaleFactor=1.15,
        minNeighbors=6,
        minSize=(int(det_w * 0.03), int(det_h * 0.05)),
        maxSize=(int(det_w * 0.22), int(det_h * 0.38)),
    )
    for fx, fy, fw, fh in faces:
      cx, cy = fx + fw / 2.0, fy + fh / 2.0
      if not (
          (0.36 * det_w < cx < 0.64 * det_w)
          and (0.32 * det_h < cy < 0.68 * det_h)
      ):
        detections.append((fx * sx, fy * sy, fw * sx, fh * sy))

  cap.release()
  if len(detections) < 3:
    return None

  med_fx = statistics.median([d[0] for d in detections])
  med_fy = statistics.median([d[1] for d in detections])
  med_fw = statistics.median([d[2] for d in detections])
  med_fh = statistics.median([d[3] for d in detections])

  cam_w = int(min(vid_w * 0.34, max(vid_w * 0.16, med_fw * 3.0)))
  cam_h = int(cam_w * 9 / 16)
  cam_x = int(max(0, min(vid_w - cam_w, (med_fx + med_fw / 2.0) - cam_w / 2.0)))
  cam_y = int(max(0, min(vid_h - cam_h, (med_fy + med_fh / 2.0) - cam_h * 0.45)))

  # Snap cleanly to outer edges if within 3% margin
  if cam_x < vid_w * 0.03:
    cam_x = 0
  elif vid_w - (cam_x + cam_w) < vid_w * 0.03:
    cam_x = vid_w - cam_w
  if cam_y < vid_h * 0.03:
    cam_y = 0
  elif vid_h - (cam_y + cam_h) < vid_h * 0.03:
    cam_y = vid_h - cam_h

  return (cam_x, cam_y, cam_w - (cam_w % 2), cam_h - (cam_h % 2))


def render_vertical_clip(raw_mp4, final_mp4, ass_path, title_text, cam_box=None):
  """Renders 1080x1920 (9:16) clip: Gameplay TOP + Webcam BOTTOM + 3.2s blurred hook title + ASS captions."""
  wrapped = "\n".join(textwrap.wrap(title_text.upper(), width=22)[:2])
  escaped_title = (
      wrapped.replace("\\", "\\\\")
      .replace(":", "\\:")
      .replace("'", "\\'")
      .replace("%", "\\%")
  )
  ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
  sub_filter = (
      f",ass='{ass_escaped}'"
      if BURN_SUBTITLES
      and os.path.exists(ass_path)
      and os.path.getsize(ass_path) > 0
      else ""
  )

  if cam_box:
    cx, cy, cw, ch = cam_box
    layout_graph = (
        "[0:v]split=2[game_src][cam_src];"
        "[game_src]scale=-2:1280,crop=1080:1280[top_game];"
        f"[cam_src]crop={cw}:{ch}:{cx}:{cy},scale=1080:640:force_original_aspect_ratio=increase,"
        "crop=1080:640[bot_cam];"
        "[top_game][bot_cam]vstack=inputs=2,split=2[base][blursrc];"
    )
  else:
    layout_graph = (
        "[0:v]split=2[bg_raw][fg_raw];"
        "[bg_raw]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=40,eq=brightness=-0.15[bg];"
        "[fg_raw]scale=1080:-2[fg];"
        "[bg][fg]overlay=0:(H-h)/2,split=2[base][blursrc];"
    )

  filter_complex = (
      f"{layout_graph}"
      "[blursrc]trim=0:3.2,setpts=PTS-STARTPTS,"
      "gblur=sigma=35,eq=brightness=-0.15,"
      f"drawtext=fontfile='{FONT_PATH}':text='{escaped_title}':"
      "fontcolor=white:fontsize=62:borderw=5:bordercolor=black:"
      "x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=16,"
      "format=yuva420p,"
      "fade=t=out:st=2.3:d=0.9:alpha=1[intro];"
      f"[base][intro]overlay=0:0:eof_action=pass{sub_filter}[vout]"
  )

  encoders = (
      ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"],
      ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"],
  )
  for vcodec_args in encoders:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        raw_mp4,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        *vcodec_args,
        "-c:a",
        "copy",
        final_mp4,
    ]
    if subprocess.run(cmd, check=False).returncode == 0:
      break


class StreamWatcher:

  def __init__(self, stream_url):
    self.stream_url = stream_url
    match = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", stream_url)
    self.channel = (
        match.group(1).lower() if match else re.sub(r"\W+", "_", stream_url)
    )
    self.buffer_dir = os.path.join(BASE_BUFFER_DIR, self.channel)
    os.makedirs(self.buffer_dir, exist_ok=True)

    self.lock = threading.Lock()
    self.chat_events = deque()
    self.audio_history = deque(maxlen=75)
    self.size_history = deque(maxlen=75)
    self.recent_scores = deque(maxlen=450)
    self.clip_timestamps = deque()

    self.dynamic_threshold = INITIAL_THRESHOLD
    self.last_tune_time = time.time()
    self.last_clip_time = 0.0

  def start(self):
    threading.Thread(target=self.run_rolling_buffer, daemon=True).start()
    threading.Thread(target=self.monitor_chat, daemon=True).start()
    threading.Thread(target=self.monitor_segments, daemon=True).start()

  def run_rolling_buffer(self):
    playlist_path = os.path.join(self.buffer_dir, "stream.m3u8")
    segment_pattern = os.path.join(self.buffer_dir, "seg_%04d.ts")

    while True:
      print(f"[{self.channel}][BUFFER] Connecting to {self.stream_url}...")
      p1 = subprocess.Popen(
          [
              "streamlink",
              "--stdout",
              "--loglevel",
              "warning",
              self.stream_url,
              "best",
          ],
          stdout=subprocess.PIPE,
      )
      p2 = subprocess.Popen(
          [
              "ffmpeg",
              "-hide_banner",
              "-loglevel",
              "error",
              "-i",
              "pipe:0",
              "-c",
              "copy",
              "-f",
              "hls",
              "-hls_time",
              str(SEGMENT_TIME),
              "-hls_list_size",
              str(BUFFER_SEGMENTS),
              "-hls_flags",
              "delete_segments",
              "-hls_segment_filename",
              segment_pattern,
              playlist_path,
          ],
          stdin=p1.stdout,
      )
      p1.stdout.close()
      p2.wait()
      shutil.rmtree(self.buffer_dir, ignore_errors=True)
      os.makedirs(self.buffer_dir, exist_ok=True)
      print(
          f"[{self.channel}][BUFFER] Offline or disconnected. Retrying in 30s..."
      )
      time.sleep(30)

  def monitor_chat(self):
    while True:
      sock = socket.socket()
      try:
        sock.settimeout(60)
        sock.connect(("irc.chat.twitch.tv", 6667))
        sock.send(b"PASS oauth:SCHMOOPIIE\r\n")
        sock.send(b"NICK justinfan54321\r\n")
        sock.send(f"JOIN #{self.channel}\r\n".encode("utf-8"))
        print(f"[{self.channel}][CHAT] Joined #{self.channel}.")

        buf = ""
        while True:
          data = sock.recv(4096).decode("utf-8", errors="ignore")
          if not data:
            raise ConnectionError("IRC closed")
          buf += data
          while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)
            if line.startswith("PING"):
              sock.send(b"PONG :tmi.twitch.tv\r\n")
            elif f"PRIVMSG #{self.channel} :" in line:
              msg_text = line.split(f"PRIVMSG #{self.channel} :", 1)[1]
              weight = 2.5 if CHAT_HYPE_PATTERN.search(msg_text) else 1.0
              now = time.time()
              with self.lock:
                self.chat_events.append((now, weight, msg_text))
                while self.chat_events and self.chat_events[0][0] < now - 120:
                  self.chat_events.popleft()
      except Exception as e:
        print(f"[{self.channel}][CHAT] Reconnecting ({e})...")
        time.sleep(5)
      finally:
        sock.close()

  def compute_chat_score(self, now):
    with self.lock:
      events = list(self.chat_events)

    msgs_60s = sum(1 for ts, _, _ in events if (now - ts) <= 60.0)
    chat_confidence = min(1.0, msgs_60s / 30.0)
    if msgs_60s < 2:
      return 0.0, 0, 0.0

    buckets = [0.0] * 24
    raw_recent = 0
    for ts, weight, _ in events:
      age = now - ts
      if age <= 5.0:
        raw_recent += 1
      idx = int(age // 5)
      if 0 <= idx < 24:
        buckets[idx] += weight

    current = buckets[0]
    baseline = buckets[1:]
    mean = statistics.mean(baseline) if baseline else 0.0
    if current < max(2.0, mean * 1.6):
      return 0.0, raw_recent, chat_confidence

    stdev = max(statistics.stdev(baseline) if len(baseline) > 1 else 0.5, 0.35)
    z_score = (current - mean) / stdev
    if z_score < 1.2:
      return 0.0, raw_recent, chat_confidence

    score = (50.0 * chat_confidence) * min(1.0, (z_score - 1.0) / 3.0)
    return score, raw_recent, chat_confidence

  def analyze_segment_signals(self, seg_path, chat_confidence, pre_score):
    """Evaluates audio & bitrate spikes, only invoking GPU Whisper if speech points can reach threshold."""
    size_kb = os.path.getsize(seg_path) / 1024.0
    with self.lock:
      avg_kb = (
          statistics.mean(self.size_history)
          if len(self.size_history) >= 10
          else size_kb
      )
      self.size_history.append(size_kb)
    size_ratio = (size_kb / avg_kb) if avg_kb > 0 else 1.0

    res = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-vn",
            "-i",
            seg_path,
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", res.stderr)
    mean_db = float(match.group(1)) if match else -50.0

    with self.lock:
      if len(self.audio_history) >= 10:
        avg_db = statistics.mean(self.audio_history)
        stdev_db = max(statistics.stdev(self.audio_history), 0.7)
      else:
        avg_db = mean_db
        stdev_db = 1.5
      self.audio_history.append(mean_db)

    db_jump = max(0.0, mean_db - avg_db)
    audio_z = db_jump / stdev_db

    av_boost = 1.0 - chat_confidence
    max_audio = 30.0 + (30.0 * av_boost)
    max_speech = 20.0 + (5.0 * av_boost)
    max_action = 15.0 * av_boost

    audio_score = (
        min(max_audio, max_audio * min(1.0, (audio_z - 1.0) / 2.5))
        if (db_jump >= 1.0 and audio_z >= 1.3)
        else 0.0
    )
    action_score = (
        min(max_action, ((size_ratio - 1.2) / 0.3) * max_action)
        if (max_action > 0 and size_ratio >= 1.25)
        else 0.0
    )

    # Gate GPU Whisper: skip transcription if max_speech cannot push total_score over threshold
    transcript = ""
    speech_score = 0.0
    if (pre_score + audio_score + action_score + max_speech) >= (
        self.dynamic_threshold - 2.0
    ):
      try:
        with gpu_lock:
          segments, _ = whisper_model.transcribe(
              seg_path, beam_size=1, vad_filter=True
          )
          transcript = " ".join(s.text.strip() for s in segments).strip()
        if transcript and SPEECH_HYPE_PATTERN.search(transcript):
          speech_score = max_speech
      except Exception as e:
        print(f"[{self.channel}][WHISPER] Error: {e}")

    return audio_score, speech_score, action_score, transcript, db_jump

  def auto_tune_threshold(self, now):
    if now - self.last_tune_time < 300:
      return
    self.last_tune_time = now

    while self.clip_timestamps and self.clip_timestamps[0] < now - 3600:
      self.clip_timestamps.popleft()

    clips_last_hour = len(self.clip_timestamps)
    old_thresh = self.dynamic_threshold

    if clips_last_hour > TARGET_CLIPS_PER_HOUR + 1:
      self.dynamic_threshold = min(MAX_THRESHOLD, self.dynamic_threshold + 4.0)
    elif clips_last_hour == 0 and len(self.recent_scores) >= 75:
      sorted_scores = sorted(self.recent_scores)
      p97 = sorted_scores[int(len(sorted_scores) * 0.97)]
      target = max(MIN_THRESHOLD, min(MAX_THRESHOLD, p97 + 5.0))
      self.dynamic_threshold = round((self.dynamic_threshold + target) / 2.0, 1)

    if self.dynamic_threshold != old_thresh:
      print(
          f"[{self.channel}][AUTO-TUNER] Threshold: {old_thresh:.1f} ->"
          f" {self.dynamic_threshold:.1f}"
      )

  def extract_and_render_clip(self, trigger_ts_str, metadata):
    with self.lock:
      recent_msgs = [msg for _, _, msg in list(self.chat_events)[-30:]]

    if CLIP_POST_SECONDS > 0:
      time.sleep(CLIP_POST_SECONDS)

    all_segments = get_sorted_segments(self.buffer_dir)
    completed = all_segments[:-1] if len(all_segments) > 1 else all_segments
    needed = max(1, (CLIP_PRE_SECONDS + CLIP_POST_SECONDS) // SEGMENT_TIME)
    selected = completed[-needed:] if len(completed) >= needed else completed
    if len(selected) < 2:
      return

    concat_path = os.path.join(self.buffer_dir, f"concat_{trigger_ts_str}.txt")
    with open(concat_path, "w", encoding="utf-8") as f:
      for seg in selected:
        f.write(f"file '{seg}'\n")

    base_name = (
        f"{self.channel}_{trigger_ts_str}_score{int(metadata['total_score'])}"
    )
    raw_mp4 = os.path.join(self.buffer_dir, f"{base_name}_raw.mp4")
    final_mp4 = os.path.join(CLIPS_DIR, f"{base_name}.mp4")
    ass_path = os.path.join(CLIPS_DIR, f"{base_name}.ass")
    json_path = os.path.join(CLIPS_DIR, f"{base_name}.json")

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-c",
            "copy",
            raw_mp4,
        ],
        check=False,
    )
    if os.path.exists(concat_path):
      os.remove(concat_path)
    if not os.path.exists(raw_mp4):
      return

    cam_box = detect_webcam_box(raw_mp4)
    layout_mode = "SPLIT-GAME-TOP-CAM-BOTTOM" if cam_box else "BLUR-CENTERED"
    metadata["layout_mode"] = layout_mode
    if cam_box:
      metadata["webcam_box"] = list(cam_box)

    full_transcript = transcribe_and_build_punchy_ass(
        raw_mp4, ass_path, has_webcam=bool(cam_box)
    )
    hook_title = generate_dynamic_title(self.channel, full_transcript, recent_msgs)
    social_meta = build_social_metadata(
        self.channel, hook_title, full_transcript
    )

    metadata["generated_title"] = hook_title
    metadata["full_transcript"] = full_transcript
    metadata["hashtags"] = social_meta["hashtags"]

    print(
        f"[{self.channel}][RENDER][{layout_mode}] Rendering 9:16"
        f" '{hook_title}'..."
    )
    render_vertical_clip(raw_mp4, final_mp4, ass_path, hook_title, cam_box=cam_box)

    if os.path.exists(raw_mp4):
      os.remove(raw_mp4)

    with open(json_path, "w", encoding="utf-8") as f:
      json.dump(metadata, f, indent=2)

    print(
        f"[{self.channel}][SAVED CLIP] {final_mp4} | Title: \"{hook_title}\" |"
        f" Tags: {' '.join(social_meta['hashtags'])}"
    )
    uploader.enqueue(final_mp4, social_meta)

  def monitor_segments(self):
    processed = set()
    while True:
      try:
        files = get_sorted_segments(self.buffer_dir)
        completed = files[:-1] if len(files) > 1 else []
        for seg_path in completed[-1:]:
          fname = os.path.basename(seg_path)
          if fname in processed:
            continue
          processed.add(fname)
          if len(processed) > 200:
            processed = {os.path.basename(p) for p in completed[-50:]}

          now = time.time()
          chat_score, raw_msgs, chat_confidence = self.compute_chat_score(now)
          audio_score, speech_score, action_score, transcript, db_jump = (
              self.analyze_segment_signals(seg_path, chat_confidence, chat_score)
          )
          total_score = chat_score + audio_score + speech_score + action_score

          self.recent_scores.append(total_score)
          self.auto_tune_threshold(now)

          mode_tag = f"CHAT:{int(chat_confidence * 100)}%"
          print(
              f"[{self.channel}][{fname}][{mode_tag}]"
              f" Score={total_score:.1f}/{self.dynamic_threshold:.1f} |"
              f" Chat={chat_score:.1f} ({raw_msgs}/5s) |"
              f" Audio={audio_score:.1f} (+{db_jump:.1f}dB) |"
              f" Speech={speech_score:.0f}"
              + (f' | "{transcript}"' if transcript else "")
          )

          if (
              total_score >= self.dynamic_threshold
              and (now - self.last_clip_time) >= COOLDOWN_SECONDS
          ):
            self.last_clip_time = now
            self.clip_timestamps.append(now)
            ts_str = time.strftime("%Y%m%d_%H%M%S")
            metadata = {
                "channel": self.channel,
                "timestamp": ts_str,
                "stream_url": self.stream_url,
                "mode": mode_tag,
                "total_score": round(total_score, 1),
                "threshold_at_trigger": round(self.dynamic_threshold, 1),
                "chat_score": round(chat_score, 1),
                "audio_score": round(audio_score, 1),
                "speech_score": speech_score,
                "action_score": round(action_score, 1),
                "trigger_transcript": transcript,
            }
            print(
                f"[{self.channel}][TRIGGER] Capturing ~30s 9:16 clip"
                f" ({total_score:.1f})..."
            )
            threading.Thread(
                target=self.extract_and_render_clip,
                args=(ts_str, metadata),
                daemon=True,
            ).start()

      except Exception as e:
        print(f"[{self.channel}][ANALYZER] Error: {e}")

      time.sleep(1.0)


if __name__ == "__main__":
  watchers = [StreamWatcher(url) for url in STREAM_URLS]
  for w in watchers:
    w.start()

  while True:
    time.sleep(60)