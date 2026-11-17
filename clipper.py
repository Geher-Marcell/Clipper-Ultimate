from collections import deque
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
from faster_whisper import WhisperModel
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

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
TITLE_MODEL_NAME = os.getenv("TITLE_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
BURN_SUBTITLES = os.getenv("BURN_SUBTITLES", "true").lower() == "true"

# Auto-Tuner & Clip Timing Settings (30s total clip: 30s pre, 0s post)
INITIAL_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "65"))
MIN_THRESHOLD = 52.0
MAX_THRESHOLD = 88.0
TARGET_CLIPS_PER_HOUR = 3.0

BASE_BUFFER_DIR = "/app/data/buffer"
CLIPS_DIR = "/app/data/clips"
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

SEGMENT_TIME = 4  # 4-second HLS chunks
BUFFER_SEGMENTS = 25  # 25 * 4s = 100s rolling buffer in RAM
CLIP_PRE_SECONDS = 30  # Reduced by 60s (90s -> 30s)
CLIP_POST_SECONDS = 0  # Reduced by 30s (30s -> 0s)
COOLDOWN_SECONDS = 45  # Prevent overlapping 30-second clips

# Modern Livestream Slang + GTA 6 Chat Triggers
CHAT_HYPE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:cooked|cinema|cinematic|ts|pmo|sybau|icl|ong|fr+|nah+|bro+|aura|unc|glaz(?:e|ing)|"
    r"type\s*shit|lock\s*in|peak|motion|valid|joever|icant|aware|pause|ayoo+|holyy+|yooo+|nooo+|"
    r"clipped?|w{3,}|l{3,}|kekw|omegalul|lulw?|pog+|poggers|omg|wtf|insane|crazy)\b"
    r"|"
    r"\b(?:ain'?t\s+no\s+way|no\s+way|hell\s+nah|we\s+are\s+so\s+back|it'?s\s+over|"
    r"chat\s+is\s+this\s+real|absolute\s+cinema|what\s+the\s+helly?)\b"
    r"|"
    r"\b(?:gta\s*(?:6|vi)|rockstar|r\*|lucia|jason|leonida|vice\s*city|trailer\s*[23]?|"
    r"delay(?:ed)?|before\s+gta\s*(?:6|vi)|goty|60\s*fps|30\s*fps|leak(?:s|ed)?)\b"
    r"|"
    r"\?{3,}|!{3,}|7{3,}"
    r")",
    re.IGNORECASE,
)

# Spoken Streamer Triggers (Whisper Transcription)
SPEECH_HYPE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:clip\s+(?:that|it)|absolute\s+cinema|we(?:'re|\s+are)\s+cooked|we\s+are\s+so\s+back|"
    r"ain'?t\s+no\s+way|no\s+way|no\s+shot|chat\s+is\s+this\s+real|what\s+just\s+happened|"
    r"oh\s+my\s+god|let'?s\s+go|are\s+you\s+(?:kidding|serious)|holy\s+shit|bro\s+what|"
    r"what\s+the\s+(?:hell|fuck)|insane|crazy)\b"
    r"|"
    r"\b(?:gta\s*(?:6|six|vi)|rockstar|lucia|jason|vice\s*city|leonida|"
    r"before\s+gta\s*(?:6|six)|game\s+of\s+the\s+year|delayed)\b"
    r")",
    re.IGNORECASE,
)

os.makedirs(BASE_BUFFER_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

print(
    f"[INIT] Loading shared Faster-Whisper ({WHISPER_MODEL_NAME}) on CUDA for"
    f" {len(STREAM_URLS)} streams..."
)
whisper_model = WhisperModel(
    WHISPER_MODEL_NAME, device="cuda", compute_type="float16"
)

print(f"[INIT] Loading local title model ({TITLE_MODEL_NAME}) on CUDA...")
title_tokenizer = AutoTokenizer.from_pretrained(TITLE_MODEL_NAME)
title_model = AutoModelForCausalLM.from_pretrained(
    TITLE_MODEL_NAME,
    torch_dtype=torch.float16,
    device_map="cuda",
)

gpu_lock = threading.Lock()


def format_srt_timestamp(seconds):
  hrs = int(seconds // 3600)
  mins = int((seconds % 3600) // 60)
  secs = int(seconds % 60)
  millis = int((seconds - int(seconds)) * 1000)
  return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def transcribe_full_clip(mp4_path, srt_path):
  """Transcribes the saved clip on GPU, writes an .srt file, and returns full text."""
  full_lines = []
  srt_blocks = []
  with gpu_lock:
    segments, _ = whisper_model.transcribe(
        mp4_path,
        beam_size=2,
        vad_filter=True,
        word_timestamps=False,
    )
    for idx, seg in enumerate(segments, start=1):
      text = seg.text.strip()
      if not text:
        continue
      full_lines.append(text)
      start_ts = format_srt_timestamp(seg.start)
      end_ts = format_srt_timestamp(seg.end)
      srt_blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{text}\n")

  with open(srt_path, "w", encoding="utf-8") as f:
    f.write("\n".join(srt_blocks))

  return " ".join(full_lines)


def generate_hook_title(channel, transcript, sample_chat_msgs):
  """Generates a 4-7 word factual hook title locally on GPU with zero API keys."""
  clean_channel = channel.replace("_", " ").title()
  messages = [
      {
          "role": "system",
          "content": (
              "You write short, punchy 4 to 7 word stream clip titles"
              " describing what happened (e.g., 'Velja gets insane 1v3"
              " clutch' or 'Streamer reacts to GTA 6 trailer'). Output ONLY"
              " the title text. No quotes, no hashtags, no emojis."
          ),
      },
      {
          "role": "user",
          "content": (
              f"Streamer: {clean_channel}\n"
              f"Transcript: {transcript[-1000:]}\n"
              f"Chat reactions: {', '.join(sample_chat_msgs[:15])}"
          ),
      },
  ]

  try:
    with gpu_lock:
      text_input = title_tokenizer.apply_chat_template(
          messages, tokenize=False, add_generation_prompt=True
      )
      inputs = title_tokenizer([text_input], return_tensors="pt").to("cuda")
      with torch.no_grad():
        output_ids = title_model.generate(
            **inputs,
            max_new_tokens=24,
            do_sample=True,
            temperature=0.4,
            pad_token_id=title_tokenizer.eos_token_id,
        )
      generated = output_ids[0][inputs.input_ids.shape[1] :]
      title = (
          title_tokenizer.decode(generated, skip_special_tokens=True)
          .strip()
          .strip("\"'")
      )
      if 3 <= len(title) <= 65:
        return title
  except Exception as e:
    print(f"[{channel}][LOCAL-TITLE] Error ({e}), using fallback.")

  combined = f"{transcript} {' '.join(sample_chat_msgs)}".lower()
  if re.search(r"gta\s*(6|vi)|rockstar|lucia|jason|vice city", combined):
    return f"{clean_channel} reacts to GTA 6"
  if re.search(r"pentakill|quadra|triple|1v\d|outplay|clutch|insane", combined):
    return f"{clean_channel} pulls off insane play"
  if re.search(r"cooked|fail|int|died|throw|no way", combined):
    return f"{clean_channel} gets completely cooked"
  return f"{clean_channel} goes crazy on stream"


def render_titled_clip(raw_mp4, final_mp4, srt_path, title_text):
  """Applies a 3.2s blurred intro card with centered bold title fading into the clip + burned subtitles."""
  wrapped = "\n".join(textwrap.wrap(title_text, width=28)[:2])
  escaped_title = (
      wrapped.replace("\\", "\\\\")
      .replace(":", "\\:")
      .replace("'", "\\'")
      .replace("%", "\\%")
  )

  srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
  sub_filter = (
      f",subtitles='{srt_escaped}':force_style='FontName=Liberation"
      " Sans,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=36'"
      if BURN_SUBTITLES
      and os.path.exists(srt_path)
      and os.path.getsize(srt_path) > 0
      else ""
  )

  filter_complex = (
      "[0:v]split=2[base][blursrc];"
      "[blursrc]trim=0:3.2,setpts=PTS-STARTPTS,"
      "gblur=sigma=35,eq=brightness=-0.12,"
      f"drawtext=fontfile='{FONT_PATH}':text='{escaped_title}':"
      "fontcolor=white:fontsize=64:borderw=4:bordercolor=black:"
      "x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=14,"
      "format=yuva420p,"
      "fade=t=out:st=2.3:d=0.9:alpha=1[intro];"
      f"[base][intro]overlay=0:0:eof_action=pass{sub_filter}[vout]"
  )

  nvenc_cmd = [
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
      "-c:v",
      "h264_nvenc",
      "-preset",
      "p4",
      "-cq",
      "22",
      "-c:a",
      "copy",
      final_mp4,
  ]
  res = subprocess.run(nvenc_cmd, check=False)
  if res.returncode != 0:
    cpu_cmd = [
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
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-c:a",
        "copy",
        final_mp4,
    ]
    subprocess.run(cpu_cmd, check=False)


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
          f"[{self.channel}][BUFFER] Offline or disconnected. Retrying in"
          " 30s..."
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
      return 0.0, 0.0, 0, 0.0

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
      return 0.0, 0.0, raw_recent, chat_confidence

    stdev = max(statistics.stdev(baseline) if len(baseline) > 1 else 0.5, 0.35)
    z_score = (current - mean) / stdev
    if z_score < 1.2:
      return 0.0, z_score, raw_recent, chat_confidence

    max_chat_pts = 50.0 * chat_confidence
    raw_ratio = min(1.0, (z_score - 1.0) / 3.0)
    return max_chat_pts * raw_ratio, z_score, raw_recent, chat_confidence

  def analyze_segment_signals(self, seg_path, chat_confidence):
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

    transcript = ""
    speech_score = 0.0
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

    return (
        audio_score,
        speech_score,
        action_score,
        transcript,
        mean_db,
        db_jump,
        audio_z,
    )

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

    if not os.path.exists(self.buffer_dir):
      return

    # Exclude the active segment currently being written by ffmpeg
    all_segments = sorted(
        [
            os.path.join(self.buffer_dir, f)
            for f in os.listdir(self.buffer_dir)
            if f.endswith(".ts")
        ],
        key=os.path.getmtime,
    )
    completed_segments = (
        all_segments[:-1] if len(all_segments) > 1 else all_segments
    )

    needed = max(
        1, (CLIP_PRE_SECONDS + CLIP_POST_SECONDS) // SEGMENT_TIME
    )  # 30 // 4 = 7 segments (~28-32s)
    selected = (
        completed_segments[-needed:]
        if len(completed_segments) >= needed
        else completed_segments
    )

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
    srt_path = os.path.join(CLIPS_DIR, f"{base_name}.srt")
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

    full_transcript = transcribe_full_clip(raw_mp4, srt_path)
    hook_title = generate_hook_title(self.channel, full_transcript, recent_msgs)
    metadata["generated_title"] = hook_title
    metadata["full_transcript"] = full_transcript

    print(
        f"[{self.channel}][RENDER] Rendering title '{hook_title}' +"
        " subtitles..."
    )
    render_titled_clip(raw_mp4, final_mp4, srt_path, hook_title)

    if os.path.exists(raw_mp4):
      os.remove(raw_mp4)

    with open(json_path, "w", encoding="utf-8") as f:
      json.dump(metadata, f, indent=2)

    print(
        f"[{self.channel}][SAVED CLIP] {final_mp4} | Title: \"{hook_title}\""
    )

  def monitor_segments(self):
    processed = set()
    while True:
      try:
        if not os.path.exists(self.buffer_dir):
          time.sleep(1)
          continue

        files = sorted(
            [f for f in os.listdir(self.buffer_dir) if f.endswith(".ts")],
            key=lambda x: os.path.getmtime(os.path.join(self.buffer_dir, x)),
        )
        completed = files[:-1] if len(files) > 1 else []
        latest_completed = completed[-1:] if completed else []

        for fname in latest_completed:
          if fname in processed:
            continue
          processed.add(fname)
          if len(processed) > 200:
            processed = set(completed[-50:])

          seg_path = os.path.join(self.buffer_dir, fname)
          if not os.path.exists(seg_path):
            continue

          now = time.time()
          chat_score, z_score, raw_msgs, chat_confidence = (
              self.compute_chat_score(now)
          )
          (
              audio_score,
              speech_score,
              action_score,
              transcript,
              mean_db,
              db_jump,
              audio_z,
          ) = self.analyze_segment_signals(seg_path, chat_confidence)
          total_score = chat_score + audio_score + speech_score + action_score

          self.recent_scores.append(total_score)
          self.auto_tune_threshold(now)

          mode_tag = f"CHAT:{int(chat_confidence * 100)}%"
          print(
              f"[{self.channel}][{fname}][{mode_tag}]"
              f" Score={total_score:.1f}/{self.dynamic_threshold:.1f} |"
              f" Chat={chat_score:.1f} ({raw_msgs}/5s) |"
              f" Audio={audio_score:.1f} (+{db_jump:.1f}dB) |"
              f' Speech={speech_score:.0f} | "{transcript}"'
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
                "chat_confidence": round(chat_confidence, 2),
                "total_score": round(total_score, 1),
                "threshold_at_trigger": round(self.dynamic_threshold, 1),
                "chat_score": round(chat_score, 1),
                "audio_score": round(audio_score, 1),
                "speech_score": speech_score,
                "action_score": round(action_score, 1),
                "trigger_transcript": transcript,
                "summary": (
                    f"{mode_tag} chat:{chat_score:.0f} audio:{audio_score:.0f}"
                    f" speech:{speech_score:.0f} action:{action_score:.0f}"
                ),
            }
            print(
                f"[{self.channel}][TRIGGER] Capturing ~30s clip"
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