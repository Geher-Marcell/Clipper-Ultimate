import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import winsound
import cv2
import imageio_ffmpeg

# Paths relative to C:\Users\Darking\Desktop\Projects\Clipper
BASE_DIR = Path(__file__).resolve().parent
CLIPS_DIR = BASE_DIR / "data" / "clips"
UPLOAD_DIR = BASE_DIR / "data" / "upload"
TEMP_WAV = Path(tempfile.gettempdir()) / "clip_reviewer_audio.wav"

# Max preview height in pixels so 1080x1920 vertical clips fit cleanly on screen
MAX_PREVIEW_HEIGHT = 860

# Keycodes for cv2.waitKeyEx() on Windows
KEY_SPACE = {32}
KEY_DELETE = {3014656, 65535, 255, 127}
KEY_PAUSE = {ord("p"), ord("P")}
KEY_REPLAY = {ord("r"), ord("R")}
KEY_SKIP = {ord("s"), ord("S"), 2555904}
KEY_QUIT = {27, ord("q"), ord("Q")}
KEY_VOL_UP = {2490368, 65362, ord("+"), ord("=")}
KEY_VOL_DOWN = {2621440, 65364, ord("-"), ord("_")}
KEY_MUTE = {ord("m"), ord("M")}


def set_volume(level: float):
    """Sets application audio session volume (0.0 to 1.0) in real time via winmm.dll."""
    level = max(0.0, min(1.0, level))
    val = int(round(level * 0xFFFF))
    stereo_val = (val << 16) | val
    try:
        ctypes.windll.winmm.waveOutSetVolume(
            ctypes.c_void_p(0), ctypes.c_uint32(stereo_val)
        )
    except Exception:
        pass


def start_audio(mp4_path: Path, volume: float, start_sec: float = 0.0):
    """Extracts audio from start_sec via bundled ffmpeg.exe and plays it via winsound."""
    stop_audio()
    if TEMP_WAV.exists():
        try:
            TEMP_WAV.unlink()
        except OSError:
            pass

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error"]
    if start_sec > 0.05:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend([
        "-i", str(mp4_path),
        "-vn", "-ac", "2", "-ar", "44100",
        str(TEMP_WAV),
    ])

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    set_volume(volume)
    if TEMP_WAV.exists() and TEMP_WAV.stat().st_size > 44:
        winsound.PlaySound(str(TEMP_WAV), winsound.SND_FILENAME | winsound.SND_ASYNC)


def stop_audio():
    """Immediately stops Windows async audio playback and releases file locks."""
    winsound.PlaySound(None, 0)


def draw_hud(frame, line1: str, line2: str):
    """Draws a readable top banner on the preview frame."""
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], 68), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)
    cv2.putText(frame, line1, (14, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(frame, line2, (14, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (0, 230, 255), 1, cv2.LINE_AA)


def review_clip(mp4_path: Path, index: int, total: int, state: dict) -> str:
    """Plays a single clip locked to the audio clock and returns 'save', 'delete', 'skip', or 'quit'."""
    json_path = mp4_path.with_suffix(".json")
    title_preview = ""
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            title_preview = meta.get("generated_title", "")
        except Exception:
            pass

    print(f"\n[{index}/{total}] {mp4_path.name}")
    if title_preview:
        print(f"   Title: {title_preview}")

    window_name = "Clip Reviewer (SPACE=Save | DEL=Delete | P=Pause | UP/DOWN=Vol | ESC=Quit)"
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

    while True:
        cap = cv2.VideoCapture(str(mp4_path))
        if not cap.isOpened():
            print(f"   [ERROR] Could not open {mp4_path.name}, skipping.")
            return "skip"

        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        if fps <= 1.0 or fps > 120.0:
            fps = 60.0
        frame_duration_ms = 1000.0 / fps

        active_vol = 0.0 if state["muted"] else state["volume"]
        start_audio(mp4_path, active_vol, start_sec=0.0)
        start_time = time.perf_counter()
        frame_idx = 0
        last_frame = None
        paused = False
        paused_pos_ms = 0.0
        finished = False
        action = None

        while True:
            vol_label = "MUTED" if state["muted"] else f"{int(round(state['volume'] * 100))}%"

            if paused:
                if last_frame is not None:
                    paused_frame = last_frame.copy()
                    draw_hud(
                        paused_frame,
                        f"[{index}/{total}] PAUSED ({paused_pos_ms / 1000.0:.1f}s) | Vol: {vol_label}",
                        "P:Resume | SPACE:Save | DEL:Del | R:Replay | S:Skip",
                    )
                    cv2.imshow(window_name, paused_frame)
                key = cv2.waitKeyEx(50)
            else:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                next_pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                if next_pos_ms <= 0 and frame_idx > 0:
                    next_pos_ms = frame_idx * frame_duration_ms

                while not finished and next_pos_ms < (elapsed_ms - 25.0):
                    if not cap.grab():
                        break
                    frame_idx += 1
                    next_pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if next_pos_ms <= 0:
                        next_pos_ms = frame_idx * frame_duration_ms

                ok, frame = cap.read() if not finished else (False, None)
                if ok:
                    frame_idx += 1
                    h, w = frame.shape[:2]
                    if h > MAX_PREVIEW_HEIGHT:
                        scale = MAX_PREVIEW_HEIGHT / h
                        frame = cv2.resize(
                            frame, (int(w * scale), MAX_PREVIEW_HEIGHT), interpolation=cv2.INTER_LINEAR
                        )

                    draw_hud(
                        frame,
                        f"[{index}/{total}] Vol: {vol_label} | {mp4_path.name[:24]}",
                        "SPACE:Save | DEL:Del | P:Pause | UP/DN:Vol | R:Replay",
                    )
                    last_frame = frame
                    cv2.imshow(window_name, frame)

                    cur_pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if cur_pos_ms <= 0:
                        cur_pos_ms = frame_idx * frame_duration_ms
                    now_ms = (time.perf_counter() - start_time) * 1000.0
                    sleep_ms = max(1, int(cur_pos_ms - now_ms))
                    key = cv2.waitKeyEx(sleep_ms)
                else:
                    finished = True
                    if last_frame is not None:
                        ended_frame = last_frame.copy()
                        draw_hud(
                            ended_frame,
                            f"[{index}/{total}] FINISHED | Vol: {vol_label}",
                            "SPACE:Save MP4 | DEL:Delete All | R:Replay | S:Skip",
                        )
                        cv2.imshow(window_name, ended_frame)
                    key = cv2.waitKeyEx(50)

            if key in KEY_PAUSE and not finished:
                if not paused:
                    paused = True
                    paused_pos_ms = max(0.0, (time.perf_counter() - start_time) * 1000.0)
                    stop_audio()
                else:
                    paused = False
                    active_vol = 0.0 if state["muted"] else state["volume"]
                    start_audio(mp4_path, active_vol, start_sec=paused_pos_ms / 1000.0)
                    start_time = time.perf_counter() - (paused_pos_ms / 1000.0)
            elif key in KEY_VOL_UP:
                state["muted"] = False
                state["volume"] = round(min(1.0, state["volume"] + 0.1), 2)
                set_volume(state["volume"])
            elif key in KEY_VOL_DOWN:
                state["muted"] = False
                state["volume"] = round(max(0.0, state["volume"] - 0.1), 2)
                set_volume(state["volume"])
            elif key in KEY_MUTE:
                state["muted"] = not state["muted"]
                set_volume(0.0 if state["muted"] else state["volume"])
            elif key in KEY_SPACE:
                action = "save"
                break
            elif key in KEY_DELETE:
                action = "delete"
                break
            elif key in KEY_REPLAY:
                action = "replay"
                break
            elif key in KEY_SKIP:
                action = "skip"
                break
            elif key in KEY_QUIT:
                action = "quit"
                break

            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                action = "quit"
                break

        stop_audio()
        cap.release()

        if action != "replay":
            return action


def main():
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not clips:
        print(f"No .mp4 clips found in {CLIPS_DIR}")
        return

    print(f"Found {len(clips)} clip(s) in {CLIPS_DIR}")
    print(f"Approved .mp4 files will be moved to: {UPLOAD_DIR}")

    state = {"volume": 0.8, "muted": False}
    saved_count = 0
    deleted_count = 0

    for idx, mp4_path in enumerate(clips, start=1):
        if not mp4_path.exists():
            continue

        decision = review_clip(mp4_path, idx, len(clips), state)

        if decision == "save":
            dest = UPLOAD_DIR / mp4_path.name
            shutil.move(str(mp4_path), str(dest))
            saved_count += 1
            print(f"   -> SAVED (moved .mp4 only to {dest.name})")

        elif decision == "delete":
            removed = []
            for ext in (".mp4", ".ass", ".json"):
                target = mp4_path.with_suffix(ext)
                if target.exists():
                    target.unlink()
                    removed.append(ext)
            deleted_count += 1
            print(f"   -> DELETED ({', '.join(removed)})")

        elif decision == "skip":
            print("   -> SKIPPED")

        elif decision == "quit":
            print("\nExiting reviewer.")
            break

    stop_audio()
    set_volume(1.0)
    cv2.destroyAllWindows()
    if TEMP_WAV.exists():
        try:
            TEMP_WAV.unlink()
        except OSError:
            pass
    print(f"\nDone! Saved: {saved_count} | Deleted: {deleted_count}")


if __name__ == "__main__":
    main()