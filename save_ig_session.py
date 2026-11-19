import os
from pathlib import Path
import re
import tkinter as tk
import urllib.parse

if os.name == "nt":
    os.system("")

OUT_PATH = Path(__file__).resolve().parent / "data" / "auth" / "ig_sessionid.txt"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_clipboard() -> str:
    root = tk.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def main():
    raw = get_clipboard()
    m = re.search(r"(?:^|[;\s'\"])sessionid=([^;'\s\"^]+)", raw)
    if not m:
        print("\033[1;91m[ERROR] No Instagram sessionid found in clipboard.\033[0m")
        print("Log into instagram.com -> F12 -> Network -> F5 -> right-click www.instagram.com -> Copy -> Copy as cURL (bash).")
        return

    sessionid = urllib.parse.unquote(m.group(1).strip())
    OUT_PATH.write_text(sessionid + "\n", encoding="utf-8")
    print(f"\033[92m[OK] Saved Instagram sessionid to: {OUT_PATH}\033[0m")


if __name__ == "__main__":
    main()