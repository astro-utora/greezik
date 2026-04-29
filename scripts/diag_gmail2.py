"""Diagnostic: print the body and extracted code for the most recent
greenhouse security-code email. Validates _extract_code logic."""

from __future__ import annotations

import datetime
import email
import imaplib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from greezik.config import load_config

load_config()

from greezik.email_verify import (  # noqa: E402
    _extract_code,
    _matches_greenhouse_verify,
    _plain_text,
)

addr = os.getenv("GMAIL_ADDRESS") or ""
pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")

imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
imap.login(addr, pw)
imap.select("INBOX", readonly=True)

since = (
    datetime.datetime.utcnow() - datetime.timedelta(hours=1)
).strftime("%d-%b-%Y")
status, data = imap.search(None, "FROM", '"greenhouse"', "SINCE", since)
ids = (data[0] or b"").split()
print(f"Search hits with broadened FROM 'greenhouse': {len(ids)}")

for mid in reversed(ids[-3:]):
    status, msg_data = imap.fetch(mid, "(RFC822)")
    raw = next(
        (e[1] for e in msg_data if isinstance(e, tuple) and len(e) >= 2),
        None,
    )
    if not raw:
        continue
    msg = email.message_from_bytes(raw)
    print()
    print("=" * 60)
    print(f"FROM:    {msg.get('From')}")
    print(f"DATE:    {msg.get('Date')}")
    print(f"SUBJECT: {msg.get('Subject')}")
    print(f"matches_verify? {_matches_greenhouse_verify(msg)}")
    body = _plain_text(msg)
    print(f"BODY (first 800 chars):")
    print(body[:800])
    print("---")
    print(f"EXTRACTED CODE: {_extract_code(msg)!r}")

imap.logout()
