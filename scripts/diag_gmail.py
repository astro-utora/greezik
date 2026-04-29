"""Quick diagnostic: list recent greenhouse.io emails in Gmail."""

from __future__ import annotations

import datetime
import email
import email.header
import imaplib
import os
import sys

# Make project root importable when run via "python scripts/diag_gmail.py".
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from greezik.config import load_config

load_config()

addr = os.getenv("GMAIL_ADDRESS") or ""
pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
print(f"Connecting as {addr} (app pw len={len(pw)})")

imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
imap.login(addr, pw)
imap.select("INBOX", readonly=True)

since = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%d-%b-%Y")
status, data = imap.search(None, "FROM", '"greenhouse.io"', "SINCE", since)
ids = (data[0] or b"").split()
print(f"Found {len(ids)} greenhouse.io emails since {since}")
for mid in reversed(ids[-5:]):
    status, msg_data = imap.fetch(mid, "(RFC822)")
    raw = next(
        (e[1] for e in msg_data if isinstance(e, tuple) and len(e) >= 2),
        None,
    )
    if not raw:
        continue
    msg = email.message_from_bytes(raw)
    chunks = email.header.decode_header(msg.get("Subject", ""))
    subj_parts = []
    for c, enc in chunks:
        if isinstance(c, bytes):
            subj_parts.append(c.decode(enc or "utf-8", errors="replace"))
        else:
            subj_parts.append(c)
    subj = "".join(subj_parts)
    print(f"  [{msg.get('Date', '?')}] FROM={(msg.get('From') or '')[:80]}")
    print(f"      SUBJ={subj[:120]}")

print()
print("--- Now searching with NO FROM filter (last 30 min) ---")
status, data = imap.search(None, "SINCE", since)
ids = (data[0] or b"").split()
print(f"Total emails since {since}: {len(ids)}")
recent = ids[-15:]
for mid in reversed(recent):
    status, msg_data = imap.fetch(mid, "(RFC822)")
    raw = next(
        (e[1] for e in msg_data if isinstance(e, tuple) and len(e) >= 2),
        None,
    )
    if not raw:
        continue
    msg = email.message_from_bytes(raw)
    chunks = email.header.decode_header(msg.get("Subject", ""))
    subj_parts = []
    for c, enc in chunks:
        if isinstance(c, bytes):
            subj_parts.append(c.decode(enc or "utf-8", errors="replace"))
        else:
            subj_parts.append(c)
    subj = "".join(subj_parts)
    sender = (msg.get("From") or "")[:80]
    if "greenhouse" in (sender + subj).lower() or "human agency" in subj.lower() or "verif" in subj.lower() or "security" in subj.lower():
        print(f"  >> [{msg.get('Date', '?')}] FROM={sender}")
        print(f"        SUBJ={subj[:120]}")

imap.logout()
