#!/usr/bin/env python3
"""
Arena & Qualer Approval Notifier
Monitors Gmail for Arena approval requests and Qualer work order emails,
posts notifications to Slack #jtb-approvals channel.
Runs via launchd every 15 minutes.
"""

import os
import sys
import json
import pickle
import re
import base64
import logging
from pathlib import Path
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Load .env from same directory as this script
load_dotenv(Path(__file__).parent / ".env")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SLACK_USER_ID = os.getenv("SLACK_USER_ID")

SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / "gmail_token.pickle"
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
PROCESSED_FILE = SCRIPT_DIR / "processed_emails.json"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_FILE}. Download credentials.json from "
                    "Google Cloud Console (OAuth 2.0 Desktop client)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Processed email tracking
# ---------------------------------------------------------------------------

def load_processed() -> set:
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(sorted(processed), f, indent=2)


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def get_message_body(msg) -> str:
    """Extract plain-text body from a Gmail message."""
    payload = msg.get("payload", {})
    parts = payload.get("parts", [])

    def decode_data(data):
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if not parts:
        data = payload.get("body", {}).get("data", "")
        return decode_data(data) if data else ""

    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return decode_data(data)

    # Fallback: first part with data
    for part in parts:
        data = part.get("body", {}).get("data", "")
        if data:
            return decode_data(data)

    return ""


def get_header(msg, name: str) -> str:
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ---------------------------------------------------------------------------
# Keyword summary (no AI)
# ---------------------------------------------------------------------------

KEYWORDS = {
    "KPI": "KPI-related",
    "compliance": "compliance",
    "SOP": "SOP",
    "equipment": "equipment",
    "calibration": "calibration",
    "validation": "validation",
    "training": "training",
    "safety": "safety",
    "audit": "audit",
    "CAPA": "CAPA",
    "change control": "change control",
    "document": "document",
}


def generate_summary(title: str) -> str:
    title_lower = title.lower()
    matched = [label for kw, label in KEYWORDS.items() if kw.lower() in title_lower]
    if matched:
        return f"Category: {', '.join(matched)}"
    return ""


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def get_slack_target() -> str:
    """Return channel ID if set, otherwise DM user ID."""
    return SLACK_CHANNEL_ID or SLACK_USER_ID


def send_slack_message(text: str):
    client = WebClient(token=SLACK_BOT_TOKEN)
    target = get_slack_target()
    try:
        client.chat_postMessage(channel=target, text=text)
        log.info(f"Slack message sent to {target}")
    except SlackApiError as e:
        log.error(f"Slack error: {e.response['error']}")
        raise


# ---------------------------------------------------------------------------
# Arena
# ---------------------------------------------------------------------------

def process_arena_emails(service, processed: set, lookback_days: int) -> list:
    query = f'from:do-not-reply@arenasolutions.com subject:"Approval Required" newer_than:{lookback_days}d'
    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])
    new_notifications = []

    for m in messages:
        msg_id = m["id"]
        if msg_id in processed:
            continue

        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        body = get_message_body(msg)
        subject = get_header(msg, "subject")

        # Parse change number e.g. AB-123456
        change_number = ""
        cn_match = re.search(r"\b([A-Z]{2}-\d+)\b", subject + " " + body)
        if cn_match:
            change_number = cn_match.group(1)

        # Parse title — first non-empty line after "Change Order" or subject fallback
        title = subject.replace("Approval Required", "").replace("-", "").strip()
        title_match = re.search(r"Title[:\s]+(.+)", body)
        if title_match:
            title = title_match.group(1).strip()

        # Parse Arena link
        link = ""
        link_match = re.search(r"(https?://app\.arenasolutions\.com/\S+)", body)
        if link_match:
            link = link_match.group(1).rstrip(".")

        summary = generate_summary(title)
        parts = [f":white_check_mark: *Arena Approval Required*"]
        if change_number:
            parts.append(f"*Change:* {change_number}")
        parts.append(f"*Title:* {title}")
        if summary:
            parts.append(summary)
        if link:
            parts.append(f"*Link:* {link}")

        message = "\n".join(parts)
        new_notifications.append((msg_id, message))
        log.info(f"Found Arena approval: {change_number or title}")

    return new_notifications


# ---------------------------------------------------------------------------
# Qualer
# ---------------------------------------------------------------------------

def process_qualer_emails(service, processed: set, lookback_days: int) -> list:
    query = f'from:mail@qualer.com subject:"Work order" subject:"was scheduled for" newer_than:{lookback_days}d'
    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])
    new_notifications = []

    for m in messages:
        msg_id = m["id"]
        if msg_id in processed:
            continue

        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        body = get_message_body(msg)
        subject = get_header(msg, "subject")

        # Parse work order number e.g. 12345-678901
        work_order = ""
        wo_match = re.search(r"\b(\d{5}-\d{6})\b", subject + " " + body)
        if wo_match:
            work_order = wo_match.group(1)

        # Parse Qualer tracking link
        link = ""
        link_match = re.search(r"(https?://\S*qualer\S+)", body)
        if link_match:
            link = link_match.group(1).rstrip(".")

        parts = [":wrench: *Qualer Work Order Scheduled*"]
        if work_order:
            parts.append(f"*Work Order:* {work_order}")
        parts.append(f"*Subject:* {subject}")
        if link:
            parts.append(f"*Link:* {link}")

        message = "\n".join(parts)
        new_notifications.append((msg_id, message))
        log.info(f"Found Qualer work order: {work_order or subject}")

    return new_notifications


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_and_notify(lookback_days: int = 1):
    log.info(f"Running with {lookback_days}-day lookback...")
    service = get_gmail_service()
    processed = load_processed()

    arena_items = process_arena_emails(service, processed, lookback_days)
    qualer_items = process_qualer_emails(service, processed, lookback_days)

    all_items = arena_items + qualer_items
    if not all_items:
        log.info("No new approval emails found.")
        return

    for msg_id, message in all_items:
        send_slack_message(message)
        processed.add(msg_id)

    save_processed(processed)
    log.info(f"Done. Sent {len(all_items)} notification(s).")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        check_and_notify(lookback_days=1)

    elif args[0] == "reset":
        if PROCESSED_FILE.exists():
            PROCESSED_FILE.unlink()
            print("Cleared processed_emails.json")
        else:
            print("Nothing to clear.")

    elif args[0] == "test":
        print("Testing Slack connection...")
        send_slack_message(":wave: arena_notifier.py test message — connection OK")
        print("Message sent.")

    elif args[0].isdigit():
        check_and_notify(lookback_days=int(args[0]))

    else:
        print(f"Unknown argument: {args[0]}")
        print("Usage: python arena_notifier.py [reset | test | <days>]")
        sys.exit(1)
