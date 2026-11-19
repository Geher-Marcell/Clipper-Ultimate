import os
from pathlib import Path
import re
import time
import tkinter as tk

if os.name == "nt":
    os.system("")

OUT_PATH = Path(__file__).resolve().parent / "data" / "auth" / "tiktok_cookies.txt"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_clipboard() -> str:
    root = tk.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def extract_cookie_header(raw: str) -> str:
    # 1. Chrome/Edge "Copy as cURL (bash)": -b 'k=v; k2=v2'
    m = re.search(r"(?:^|\s)-b\s+\$?'([^']+)'", raw)
    if m:
        return m.group(1)

    # 2. Alternative cURL header format: -H 'cookie: k=v; k2=v2'
    m = re.search(r"-H\s+\$?'cookie:\s*([^']+)'", raw, re.IGNORECASE)
    if m:
        return m.group(1)

    # 3. Chrome/Edge "Copy as cURL (cmd)" format
    m = re.search(r'(?:^|\s)-b\s+\^"(.+?)\^"', raw)
    if m:
        return m.group(1).replace("^", "")
    m = re.search(r'-H\s+\^"cookie:\s*(.+?)\^"', raw, re.IGNORECASE)
    if m:
        return m.group(1).replace("^", "")

    # 4. Raw cookie value fallback (copied from Request Headers -> cookie)
    if "sessionid=" in raw:
        return raw.strip()

    return ""


def main():
    raw = get_clipboard()
    cookie_str = extract_cookie_header(raw)

    if not cookie_str or "sessionid=" not in cookie_str:
        print("\033[1;91m[ERROR] No logged-in TikTok session cookies found in clipboard.\033[0m")
        print("Log into tiktok.com -> F12 -> Network -> F5 -> right-click www.tiktok.com -> Copy -> Copy as cURL (bash).")
        return

    expiry = int(time.time()) + 365 * 24 * 3600
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from Browser DevTools",
        "",
    ]

    count = 0
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            lines.append(f".tiktok.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
            count += 1

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\033[92m[OK] Saved {count} TikTok cookies to: {OUT_PATH}\033[0m")


if __name__ == "__main__":
    main()