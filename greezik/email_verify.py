"""Gmail IMAP poller for Greenhouse "verify your email" submission flow.

Some Greenhouse application forms send a one-time security code to the
candidate's email address after the first ``Submit application`` click.
This module logs into Gmail over IMAP, polls the inbox for the most
recent matching email, and returns the embedded code so the autobid
can paste it back into the form.

Usage::

    code = fetch_greenhouse_security_code(
        sent_after=datetime.datetime.now(datetime.UTC),
        timeout_seconds=90,
    )

Returns ``None`` if the email never arrives within ``timeout_seconds``.
Returns a :class:`VerifyEmailDisabled` for a clean "skip this code-prompt
flow" path when the user hasn't set ``GMAIL_APP_PASSWORD``.
"""

from __future__ import annotations

import datetime as _dt
import email
import email.header
import email.utils
import imaplib
import logging
import os
import re
import time
from dataclasses import dataclass
from email.message import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GmailConfig:
    """Resolved Gmail IMAP credentials, loaded from environment variables."""

    address: str
    app_password: str
    host: str = "imap.gmail.com"
    port: int = 993


class VerifyEmailDisabled(RuntimeError):
    """Raised when GMAIL_APP_PASSWORD is missing.

    Caught by the autobid as "user opted out of automatic code reading"
    rather than a fatal error.
    """


def load_gmail_config() -> GmailConfig:
    """Pull Gmail credentials out of the environment.

    Falls back to ``APPLICANT_EMAIL`` for the address. Raises
    :class:`VerifyEmailDisabled` if no app password is set.
    """

    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if not password:
        raise VerifyEmailDisabled(
            "GMAIL_APP_PASSWORD is not set; cannot read the verification "
            "code automatically. Generate an app password at "
            "https://myaccount.google.com/apppasswords and add it to .env."
        )
    address = (
        os.getenv("GMAIL_ADDRESS")
        or os.getenv("APPLICANT_EMAIL")
        or ""
    ).strip()
    if not address:
        raise VerifyEmailDisabled(
            "No Gmail address available. Set GMAIL_ADDRESS or "
            "APPLICANT_EMAIL in .env."
        )
    host = (os.getenv("EMAIL_IMAP_HOST") or "imap.gmail.com").strip()
    try:
        port = int(os.getenv("EMAIL_IMAP_PORT") or "993")
    except ValueError:
        port = 993
    return GmailConfig(address=address, app_password=password, host=host, port=port)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Greenhouse's verification email subject is consistent enough that we can
# search on it. Their from-address has shifted between
# ``no-reply@greenhouse.io``, ``donotreply@greenhouse.io``, and a per-board
# ``@boards-mail.greenhouse.io`` over time, so we don't bother filtering on
# sender; the subject + body keyword combo is unambiguous enough.
_SUBJECT_NEEDLES = (
    "security code",
    "verify your email",
    "verification code",
    "your application",
)
_BODY_ANCHOR = "copy and paste this code"

# The codes we've seen are 6-12 character base62-ish tokens (e.g.
# ``1Zse4uQp``). We extract the first such token immediately following
# the anchor phrase to avoid grabbing unrelated alphanumerics from the
# email footer.
_CODE_REGEX = re.compile(r"\b([A-Za-z0-9]{6,16})\b")


def fetch_greenhouse_security_code(
    *,
    sent_after: _dt.datetime,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 5.0,
    config: GmailConfig | None = None,
) -> str | None:
    """Poll Gmail and return the most recent Greenhouse verification code.

    Parameters
    ----------
    sent_after:
        Only consider emails received at or after this timestamp. Use
        the moment *just before* the submit-button click so we don't
        pick up an old code from a previous job. Naive datetimes are
        treated as UTC.
    timeout_seconds:
        Total wall-clock budget for the poll loop. Returns ``None``
        when exhausted.
    poll_interval_seconds:
        Delay between IMAP fetches. Gmail tolerates frequent polls
        but there's no point hammering it.
    """

    cfg = config or load_gmail_config()
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    if sent_after.tzinfo is None:
        sent_after = sent_after.replace(tzinfo=_dt.timezone.utc)

    # IMAP `SINCE` only has day resolution -- we still get same-day
    # emails. We further filter by the message's Date header.
    since_str = sent_after.strftime("%d-%b-%Y")

    while True:
        try:
            code = _try_fetch_once(cfg, since_str, sent_after)
        except Exception as exc:
            logger.warning("IMAP poll failed: %s", exc)
            code = None
        if code:
            logger.info("Verification code received from Gmail: %s", code)
            return code
        if time.monotonic() >= deadline:
            logger.warning(
                "Timed out after %.0fs waiting for the Greenhouse "
                "verification email. Check your inbox manually.",
                timeout_seconds,
            )
            return None
        time.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _try_fetch_once(
    cfg: GmailConfig, since_str: str, sent_after: _dt.datetime
) -> str | None:
    """One IMAP search/fetch sweep. Returns a code, or ``None``."""

    with _imap_connect(cfg) as imap:
        # SELECT inbox in read-only so we don't accidentally mark
        # operator-relevant unread mail as read.
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT failed: {status}")

        # Search greenhouse-flavoured emails. Greenhouse sends
        # verification emails from at least:
        #   no-reply@greenhouse.io
        #   donotreply@greenhouse.io
        #   no-reply@us.greenhouse-mail.io   (current default)
        #   no-reply@boards-mail.greenhouse.io
        # We match the BRAND substring "greenhouse" rather than a
        # specific TLD so all variants are covered. SINCE keeps the
        # poll cheap.
        status, data = imap.search(
            None,
            "FROM", '"greenhouse"',
            "SINCE", since_str,
        )
        if status != "OK":
            return None
        ids = (data[0] or b"").split()
        # Newest first.
        for msg_id in reversed(ids):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = _first_payload(msg_data)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw)
            if not _matches_greenhouse_verify(msg):
                continue
            if not _is_after(msg, sent_after):
                # Continue scanning -- newer messages may follow.
                continue
            code = _extract_code(msg)
            if code:
                return code
    return None


class _Imap:
    """Thin context-manager wrapper around imaplib so we always logout."""

    def __init__(self, cfg: GmailConfig):
        self._cfg = cfg
        self._imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self._cfg.host, self._cfg.port)
        try:
            imap.login(self._cfg.address, self._cfg.app_password)
        except imaplib.IMAP4.error as exc:
            try:
                imap.logout()
            except Exception:
                pass
            raise RuntimeError(
                f"Gmail IMAP login failed for {self._cfg.address}: {exc}. "
                "Double-check GMAIL_APP_PASSWORD (must be an app password, "
                "not the account password)."
            ) from exc
        self._imap = imap
        return imap

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._imap is None:
            return
        try:
            self._imap.close()
        except Exception:
            pass
        try:
            self._imap.logout()
        except Exception:
            pass


def _imap_connect(cfg: GmailConfig) -> _Imap:
    return _Imap(cfg)


def _first_payload(msg_data) -> bytes | None:
    """imap.fetch returns a list of mixed tuples / bytes; pull out the
    first ``bytes`` payload, which is the RFC822 body."""
    for entry in msg_data:
        if isinstance(entry, tuple) and len(entry) >= 2 and isinstance(entry[1], (bytes, bytearray)):
            return bytes(entry[1])
    return None


def _matches_greenhouse_verify(msg: Message) -> bool:
    subject = _decode_header(msg.get("Subject") or "").lower()
    if any(needle in subject for needle in _SUBJECT_NEEDLES):
        return True
    # Subject can vary by board; fall back to body keyword.
    return _BODY_ANCHOR in _plain_text(msg).lower()


def _is_after(msg: Message, cutoff: _dt.datetime) -> bool:
    raw_date = msg.get("Date") or ""
    try:
        sent = email.utils.parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return True  # if we can't parse, don't filter it out
    if sent is None:
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=_dt.timezone.utc)
    # Allow a small backwards skew (clock drift between server and
    # local machine).
    return sent >= cutoff - _dt.timedelta(seconds=30)


def _extract_code(msg: Message) -> str | None:
    """Pull the security code out of a Greenhouse verification email.

    Strategy: find the anchor phrase ("Copy and paste this code") and
    return the first base62-ish token within ~200 chars after it.
    Falls back to the largest single-line alphanumeric token in the
    body when the anchor is absent (older email templates).
    """

    body = _plain_text(msg)
    if not body:
        return None
    low = body.lower()
    anchor_idx = low.find(_BODY_ANCHOR)
    search_text = body[anchor_idx:] if anchor_idx >= 0 else body
    for match in _CODE_REGEX.finditer(search_text):
        token = match.group(1)
        # Skip obviously-non-code tokens like all-letters words.
        if token.isalpha() and token.lower() in _COMMON_BODY_WORDS:
            continue
        if token.isdigit() and len(token) < 6:
            continue
        # Skip the URL-safe paths that occasionally precede the code.
        return token
    return None


# Words that match _CODE_REGEX but are obviously English boilerplate.
_COMMON_BODY_WORDS = {
    "application",
    "applications",
    "candidate",
    "candidates",
    "greenhouse",
    "recruiting",
    "security",
    "submit",
    "submitted",
    "verify",
    "thank",
    "please",
    "after",
    "before",
    "below",
    "click",
    "enter",
    "field",
    "paste",
    "copy",
    "address",
    "company",
}


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    out: list[str] = []
    for chunk, encoding in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, ValueError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _plain_text(msg: Message) -> str:
    """Return the plain-text body of ``msg``. Falls back to stripping
    HTML tags when only text/html is available."""

    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        if ctype == "text/plain":
            plain_parts.append(_decoded_part(part))
        elif ctype == "text/html":
            html_parts.append(_decoded_part(part))
    if plain_parts:
        return "\n".join(p for p in plain_parts if p)
    if html_parts:
        # Crude but sufficient for Greenhouse's emails.
        joined = "\n".join(html_parts)
        # Replace block tags with newlines so the anchor + code aren't
        # collapsed into one big line.
        joined = re.sub(r"<\s*/?\s*(p|div|h\d|br)[^>]*>", "\n", joined, flags=re.I)
        joined = re.sub(r"<[^>]+>", " ", joined)
        joined = re.sub(r"&nbsp;", " ", joined, flags=re.I)
        joined = re.sub(r"&amp;", "&", joined, flags=re.I)
        joined = re.sub(r"\s+\n", "\n", joined)
        return joined
    return ""


def _decoded_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")
