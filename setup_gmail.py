#!/usr/bin/env python3
"""
Run this once to authorize Gmail access and generate gmail_token.pickle.
Requires credentials.json downloaded from Google Cloud Console.
"""

import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SCRIPT_DIR = Path(__file__).parent

credentials_file = SCRIPT_DIR / "credentials.json"
token_file = SCRIPT_DIR / "gmail_token.pickle"

if not credentials_file.exists():
    print("ERROR: credentials.json not found.")
    print("Download it from Google Cloud Console:")
    print("  APIs & Services → Credentials → OAuth 2.0 Client ID (Desktop) → Download JSON")
    print(f"  Save as: {credentials_file}")
    exit(1)

print("Opening browser for Gmail authorization...")
flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
creds = flow.run_local_server(port=0)

with open(token_file, "wb") as f:
    pickle.dump(creds, f)

print(f"Authorization complete. Token saved to {token_file}")
