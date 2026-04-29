"""Shared user-facing alerts (Windows MessageBox + console fallback).

The bot occasionally needs to pause and ask the human to take over --
typically when an application's submit step fails (silent failure,
form validation we don't recognise, email-verify code we couldn't read,
etc.). This module centralises the OS-native alert plumbing so both
the live runner (``applier.py``) and the standalone tester
(``test_autobid.py``) show the same dialogs.
"""

from __future__ import annotations

import logging
import sys
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)


# Windows MessageBoxW style flags we use. (Defined here so callers
# don't need to import ctypes themselves.)
_MB_OK = 0x00000000
_MB_OKCANCEL = 0x00000001
_MB_ICONWARNING = 0x00000030
_MB_ICONINFORMATION = 0x00000040
_MB_TOPMOST = 0x00040000
_MB_SETFOREGROUND = 0x00010000
_IDOK = 1
_IDCANCEL = 2


def short_url_for_alert(url: str) -> str:
    """Trim a long apply URL down to a recognisable preview.

    Greenhouse embed URLs are 150+ chars and wrap awkwardly inside the
    Windows MessageBox -- showing the host plus the company / token
    keeps the dialog readable.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc or ""
    qs = parse_qs(parts.query or "")
    company = (qs.get("for") or [""])[0]
    token = (qs.get("token") or [""])[0]
    if host and company:
        if token:
            return f"{host}/...?for={company}&token={token}"
        return f"{host}/...?for={company}"
    if host:
        return f"{host}{parts.path}"
    return url[:120]


def manual_apply_alert(
    *,
    role: str,
    company: str,
    url: str,
    head: str,
    detail_lines: list[str] | None = None,
) -> bool:
    """Block until the user decides whether they finished the apply.

    Returns
    -------
    True
        User clicked **OK** -- treat the job as applied (they finished it
        manually in the open browser tab).
    False
        User clicked **Cancel** -- record the job as skipped.

    On Windows we pop a real ``MessageBoxW`` so the dialog grabs focus
    over the Chromium window. Non-Windows / failure path uses a stdin
    prompt instead.
    """

    short_url = short_url_for_alert(url)
    role_label = role or "(role unknown)"
    company_label = company or "(company unknown)"
    title = f"Greezik: manual apply needed -- {company_label}"
    detail = "\n".join(f" - {line}" for line in (detail_lines or [])) or "(no extra detail)"
    body = (
        f"{head}\n\n"
        f"Role:    {role_label}\n"
        f"Company: {company_label}\n"
        f"URL:     {short_url}\n\n"
        f"Detail:\n{detail}\n\n"
        f"Finish the application MANUALLY in the open browser tab.\n"
        f"Click OK once you've submitted (it will be recorded as APPLIED).\n"
        f"Click Cancel to skip this job (it will be recorded as SKIPPED)."
    )

    logger.warning(
        "Manual apply needed for %s @ %s -- pausing for user input.",
        role_label,
        company_label,
    )

    if sys.platform == "win32":
        try:
            import ctypes

            style = (
                _MB_OKCANCEL
                | _MB_ICONWARNING
                | _MB_TOPMOST
                | _MB_SETFOREGROUND
            )
            rc = ctypes.windll.user32.MessageBoxW(0, body, title, style)
            if rc == _IDOK:
                return True
            return False
        except Exception as exc:
            logger.warning(
                "Could not show MessageBox (%s); falling back to console prompt.",
                exc,
            )

    print()
    print("=" * 60)
    print(f"MANUAL APPLY NEEDED -- {role_label} @ {company_label}")
    print(head)
    print(detail)
    print(f"URL: {short_url}")
    print(
        "Press Enter once you've submitted (records as APPLIED), "
        "or type 's' + Enter to skip (records as SKIPPED): ",
        end="",
        flush=True,
    )
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "s"
    return ans != "s"


def submit_failure_alert(
    *,
    index: int,
    total: int,
    url: str,
    head: str,
    detail_lines: list[str] | None = None,
) -> None:
    """Pop a Windows alert when a submit attempt did NOT confirm.

    Two-button OK/Cancel; OK advances, Cancel raises ``SystemExit(0)``
    so the caller's outer loop breaks cleanly. Used by the standalone
    test runner; the live runner uses :func:`manual_apply_alert`
    instead because it has more nuanced handling of the choice.
    """

    short_url = short_url_for_alert(url)
    title = f"Greezik SUBMIT FAILED [{index}/{total}]"
    detail = "\n".join(f" - {line}" for line in (detail_lines or [])) or "(no extra detail)"
    body = (
        f"{head}\n\n"
        f"{detail}\n\n"
        f"URL: {short_url}\n\n"
        f"Finish the application MANUALLY in the browser,\n"
        f"then click OK to move to the next job.\n"
        f"Click Cancel to abort the test run."
    )

    logger.warning("Submit failed for %s -- pausing for manual fix.", short_url)
    if sys.platform == "win32":
        try:
            import ctypes

            style = (
                _MB_OKCANCEL
                | _MB_ICONWARNING
                | _MB_TOPMOST
                | _MB_SETFOREGROUND
            )
            rc = ctypes.windll.user32.MessageBoxW(0, body, title, style)
            if rc == _IDCANCEL:
                logger.info("User aborted the test run from submit-failure prompt.")
                raise SystemExit(0)
            return
        except SystemExit:
            raise
        except Exception as exc:
            logger.warning(
                "Could not show submit-failure MessageBox (%s); "
                "falling back to console prompt.",
                exc,
            )

    print()
    print("=" * 60)
    print(f"SUBMIT FAILED [{index}/{total}] -- {head}")
    print(detail)
    print(f"URL: {short_url}")
    print("Press Enter to continue, or type 'q' + Enter to abort: ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "q"
    if ans == "q":
        logger.info("User aborted the test run from submit-failure prompt.")
        raise SystemExit(0)


def review_pause_alert(
    *,
    index: int,
    total: int,
    url: str,
    body_lead: str,
    icon_warning: bool = False,
) -> None:
    """Per-job review pause used by the test runner. OK = next job,
    Cancel = abort run."""

    short_url = short_url_for_alert(url)
    title = f"Greezik review [{index}/{total}]"
    body = (
        f"{body_lead}\n\n"
        f"URL: {short_url}\n\n"
        f"Click OK in the BROWSER first to verify the fields,\n"
        f"then click OK here to move to the next job.\n"
        f"Click Cancel to abort the test run."
    )

    logger.info("Pausing for manual review of job %d/%d ...", index, total)

    if sys.platform == "win32":
        try:
            import ctypes

            style = (
                _MB_OKCANCEL
                | (_MB_ICONWARNING if icon_warning else _MB_ICONINFORMATION)
                | _MB_TOPMOST
                | _MB_SETFOREGROUND
            )
            rc = ctypes.windll.user32.MessageBoxW(0, body, title, style)
            if rc == _IDCANCEL:
                logger.info("User aborted the test run from review prompt.")
                raise SystemExit(0)
            return
        except SystemExit:
            raise
        except Exception as exc:
            logger.warning(
                "Could not show Windows MessageBox (%s); "
                "falling back to console prompt.",
                exc,
            )

    print()
    print("=" * 60)
    print(f"REVIEW [{index}/{total}] -- {body_lead}")
    print(f"URL: {short_url}")
    print("Press Enter to continue, or type 'q' + Enter to abort: ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "q"
    if ans == "q":
        logger.info("User aborted the test run from review prompt.")
        raise SystemExit(0)
