#!/usr/bin/env python3
"""One-off OAuth handshake to mint a refresh token for Google Postmaster Tools.

JOSH RUNS THIS LOCALLY ONCE, paste the refresh token to Claude when prompted.
Claude then stores it in Supabase Vault and the sidecar uses it to mint
short-lived access tokens on every poll.

Prerequisites:
1. In Google Cloud Console (signed in as josh.gain@reachos.co):
   - Create project: reachos-postmaster
   - Enable the "Postmaster Tools API"
   - Create OAuth 2.0 Client ID:
       Application type: Desktop app  (NOT Web - simpler for this one-off)
       Name: postmaster-bootstrap
   - Download the client JSON file to your local machine

2. Save the downloaded JSON as:  ./gpt_oauth_client.json
   (in the same directory as this script)

3. Install deps:  pip install google-auth-oauthlib

4. Run:  python gpt_oauth_bootstrap.py

The script opens your browser for the consent screen, you grant
"View Postmaster Tools data" permission, the script captures the
refresh token and prints it.

Paste the printed REFRESH_TOKEN line back to Claude. That's it - this
script never needs to run again unless the refresh token is revoked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dep. Run: pip install google-auth-oauthlib", file=sys.stderr)
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/postmaster.readonly"]
CLIENT_FILE = Path(__file__).parent / "gpt_oauth_client.json"

if not CLIENT_FILE.exists():
    print(f"Missing {CLIENT_FILE}. Download from Google Cloud Console (see header docstring).", file=sys.stderr)
    sys.exit(1)

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
# access_type=offline gives us the refresh token; prompt=consent forces it on every
# re-auth so we don't get caught out by Google silently dropping it.
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

if not creds.refresh_token:
    print("No refresh token returned. Re-run with the OAuth consent UI - did you grant offline access?", file=sys.stderr)
    sys.exit(2)

# Read client_id + client_secret from the same JSON so Claude can store all three
client_data = json.loads(CLIENT_FILE.read_text())
client_id = client_data.get("installed", {}).get("client_id") or client_data.get("web", {}).get("client_id")
client_secret = client_data.get("installed", {}).get("client_secret") or client_data.get("web", {}).get("client_secret")

print("\n" + "=" * 60)
print("PASTE THE 3 LINES BELOW TO CLAUDE:")
print("=" * 60)
print(f"GPT_CLIENT_ID={client_id}")
print(f"GPT_CLIENT_SECRET={client_secret}")
print(f"GPT_REFRESH_TOKEN={creds.refresh_token}")
print("=" * 60)
print("\nThis script can be deleted after Claude confirms the values work.")
