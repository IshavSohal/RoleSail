"""Opt-in Gmail OAuth connection and creation of unsent, user-scheduled drafts."""

from __future__ import annotations

import base64
import html
import json
import os
import tempfile
from email.message import EmailMessage
from pathlib import Path

from rolesail import config

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


def _html_body(body_text: str) -> str:
    """Give Gmail flowing paragraphs while preserving intentional line breaks."""
    body = body_text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = body.split("\n\n")
    return "<div>" + "</div><div><br></div><div>".join(
        html.escape(paragraph).replace("\n", "<br>") for paragraph in paragraphs
    ) + "</div>"


def _google_modules():
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Gmail support is not installed. Run: pip install 'rolesail[gmail]'") from exc
    return Credentials, InstalledAppFlow, build


def _write_private(path: Path, content: str) -> None:
    """Atomically persist an OAuth secret with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".gmail-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def connected_account() -> str | None:
    """Return the account selected at connection time, without a network request."""
    if not config.GMAIL_TOKEN_PATH.exists() or not config.GMAIL_ACCOUNT_PATH.exists():
        return None
    try:
        return str(json.loads(config.GMAIL_ACCOUNT_PATH.read_text(encoding="utf-8"))["email"]).strip() or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def connect_gmail(credentials_path: Path) -> str:
    """Run Google's loopback OAuth consent flow for the selected personal Gmail account."""
    if not credentials_path.is_file():
        raise ValueError(f"OAuth client JSON was not found: {credentials_path}")
    _, InstalledAppFlow, build = _google_modules()
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), [GMAIL_COMPOSE_SCOPE])
    credentials = flow.run_local_server(host="localhost", port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return an offline refresh token. Reconnect and grant consent again.")
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    email = str(service.users().getProfile(userId="me").execute()["emailAddress"]).strip().lower()
    if not email:
        raise RuntimeError("Google did not identify the connected Gmail account")
    _write_private(config.GMAIL_TOKEN_PATH, credentials.to_json())
    _write_private(config.GMAIL_ACCOUNT_PATH, json.dumps({"email": email}))
    return email


class GmailDraftClient:
    """Create drafts only. This class deliberately has no send method."""

    def __init__(self) -> None:
        expected = connected_account()
        if not expected:
            raise RuntimeError("Gmail is not connected. Run: rolesail gmail-connect --credentials PATH")
        Credentials, _, build = _google_modules()
        try:
            credentials = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_PATH), [GMAIL_COMPOSE_SCOPE])
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            self.email = str(self._service.users().getProfile(userId="me").execute()["emailAddress"]).strip().lower()
        except Exception as exc:
            raise RuntimeError("Could not access Gmail. Re-run rolesail gmail-connect to refresh the connection.") from exc
        if self.email != expected:
            raise RuntimeError("Connected Gmail account changed. Re-run rolesail gmail-connect before creating drafts.")

    def create_draft(self, *, recipient_email: str, subject: str, body_text: str) -> str:
        message = EmailMessage()
        message["From"] = self.email
        message["To"] = recipient_email
        message["Subject"] = subject
        message.set_content(body_text)
        message.add_alternative(_html_body(body_text), subtype="html")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = self._service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        draft_id = str(result.get("id") or "")
        if not draft_id:
            raise RuntimeError("Gmail did not return a draft ID; check Gmail Drafts before retrying")
        return draft_id
