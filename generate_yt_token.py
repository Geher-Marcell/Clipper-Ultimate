import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

if os.name == "nt":
    os.system("")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = Path.home() / "Downloads"
TOKEN_OUT = BASE_DIR / "data" / "auth" / "youtube_token.json"

# Search both the project folder and Downloads for any client_secret*.json
candidates = list(BASE_DIR.glob("client_secret*.json")) + list(
    DOWNLOADS_DIR.glob("client_secret*.json")
)

if not candidates:
    print(
        "\033[1;91m[ERROR] No client_secret*.json found in Clipper or Downloads.\033[0m\n"
        "Download your Desktop App OAuth JSON from Google Cloud Console -> Credentials first."
    )
    raise SystemExit(1)

# Pick the most recently modified/downloaded matching JSON
client_secret_file = max(candidates, key=lambda p: p.stat().st_mtime)
print(f"Using OAuth client file: {client_secret_file}")

TOKEN_OUT.parent.mkdir(parents=True, exist_ok=True)

flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
creds = flow.run_local_server(port=0)

TOKEN_OUT.write_text(creds.to_json(), encoding="utf-8")
print(f"\033[92m[OK] Saved YouTube token to: {TOKEN_OUT}\033[0m")