"""Backfill ``big_log.jsonl`` from the jobright "Applied" page.

The :func:`run_backfill_loop` driver navigates to
``https://jobright.ai/jobs/applied`` and walks every visible card
top-to-bottom. For each card we read the role title, company name,
and "X hours/days/weeks/months ago" publish-time tag, append a
single JSON line to the configured ``APPLIED_URLS_FILE``, and click
the card's X / remove-card button so the next card slides up into
the top slot. The loop terminates as soon as a card's publish time
is older than ``BACKFILL_STOP_AFTER`` -- so the file is filled with
the most recent N applied jobs without scraping the user's entire
history.

The records written here intentionally use ``external_url=""`` and
``submission_status="verified"`` because the Applied page does NOT
expose the underlying portal URL: we know the application was
submitted (jobright tracks it) but we can't link out to it. The
``applied_at`` timestamp is computed as ``run_started_utc -
publish_offset`` so the JSONL file's chronology lines up with the
publish-time tags we saw on screen.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import (
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from . import selectors

logger = logging.getLogger(__name__)


# Approximate magnitudes used to convert relative-time strings into a
# ``timedelta``. "month" and "year" are inherently fuzzy on a calendar
# basis; we use the conventional 30-day month / 365-day year so a
# threshold like ``5m`` maps to ~5 calendar months without depending
# on a heavyweight date library.
_UNIT_TO_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "month": 30 * 24 * 60 * 60,
    "year": 365 * 24 * 60 * 60,
}

# "22 hours ago", "1 day ago", "an hour ago", ...
_PUBLISH_RE = re.compile(
    r"^\s*(\d+|a|an)\s+(second|minute|hour|day|week|month|year)s?\s+ago\s*$",
    re.IGNORECASE,
)

# "2h", "30min", "1d", "3w", "5m", "1y", ...
_THRESHOLD_RE = re.compile(
    r"^\s*(\d+)\s*([a-z]+)\s*$",
    re.IGNORECASE,
)


def parse_publish_time(text: str) -> timedelta | None:
    """Parse a jobright publish-time tag into an offset before "now".

    Recognises ``"<n> {seconds|minutes|hours|days|weeks|months|years} ago"``
    (case-insensitive, optional plural ``s``) and the conversational
    ``"a hour ago"`` / ``"an hour ago"`` forms. ``"Just now"`` (and a
    few similar phrasings) collapse to a zero offset.

    Returns ``None`` for unparseable / empty input so callers can
    distinguish "freshly published" from "we couldn't read the tag".
    """
    if not isinstance(text, str):
        return None
    raw = text.strip().lower()
    if not raw:
        return None
    if raw in ("just now", "moments ago", "a moment ago", "a few seconds ago"):
        return timedelta(0)
    m = _PUBLISH_RE.match(raw)
    if not m:
        return None
    qty_raw, unit = m.group(1), m.group(2).lower()
    qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
    return timedelta(seconds=_UNIT_TO_SECONDS[unit] * qty)


# Map of every accepted unit suffix in the threshold env var to its
# canonical key in ``_UNIT_TO_SECONDS``. The bare letter ``m`` is
# deliberately bound to **months** (not minutes) so the user's
# example values (``2h``, ``1d``, ``3w``, ``5m``) match the publish-time
# tag's vocabulary; ``min`` / ``minutes`` is the explicit minute spelling.
_THRESHOLD_UNITS: dict[str, str] = {
    "s": "second",
    "sec": "second",
    "second": "second",
    "seconds": "second",
    "min": "minute",
    "mins": "minute",
    "minute": "minute",
    "minutes": "minute",
    "h": "hour",
    "hr": "hour",
    "hrs": "hour",
    "hour": "hour",
    "hours": "hour",
    "d": "day",
    "day": "day",
    "days": "day",
    "w": "week",
    "wk": "week",
    "wks": "week",
    "week": "week",
    "weeks": "week",
    "m": "month",
    "mo": "month",
    "mos": "month",
    "month": "month",
    "months": "month",
    "y": "year",
    "yr": "year",
    "yrs": "year",
    "year": "year",
    "years": "year",
}


def parse_stop_after(spec: str) -> timedelta | None:
    """Parse a ``BACKFILL_STOP_AFTER`` spec like ``"3w"`` or ``"5m"``.

    Returns ``None`` for unparseable / empty input so the caller can
    fall back to a sane default rather than silently iterating the
    entire applied-jobs history.
    """
    if not isinstance(spec, str):
        return None
    m = _THRESHOLD_RE.match(spec)
    if not m:
        return None
    qty = int(m.group(1))
    unit_raw = m.group(2).lower()
    unit = _THRESHOLD_UNITS.get(unit_raw)
    if unit is None:
        return None
    return timedelta(seconds=_UNIT_TO_SECONDS[unit] * qty)


@dataclass
class BackfillStats:
    """Outcome counters for one ``run_backfill_loop`` invocation."""

    recorded: int = 0
    unparsed_publish: int = 0
    removed: int = 0


def run_backfill_loop(
    page: Page,
    *,
    applied_file: Path,
    stop_after_spec: str,
    action_timeout_ms: int,
    source: str = "jobright.ai/jobs/applied",
    provider: str = "jobright",
    submission_status: str = "verified",
) -> BackfillStats:
    """Walk the /jobs/applied feed, append one record per card.

    ``applied_file`` is opened in append mode -- the existing log is
    never rewritten -- and missing parent directories are created.
    The loop stops at the first card whose publish-time exceeds
    ``stop_after_spec`` (or when the feed runs out, or when the X
    button click stops shrinking the feed).
    """

    stats = BackfillStats()

    stop_after = parse_stop_after(stop_after_spec)
    if stop_after is None:
        logger.warning(
            "Could not parse BACKFILL_STOP_AFTER=%r; defaulting to 1 week.",
            stop_after_spec,
        )
        stop_after = timedelta(weeks=1)
    logger.info(
        "Backfill stop threshold: %s (raw=%r)",
        _format_timedelta(stop_after),
        stop_after_spec,
    )

    logger.info("Navigating to %s", selectors.APPLIED_URL)
    page.goto(selectors.APPLIED_URL, wait_until="domcontentloaded")

    # Anchor every record off a single "now" so the chronology of the
    # appended lines mirrors the order of cards we walked.
    run_started = datetime.now(timezone.utc)
    logger.info(
        "Anchor timestamp for applied_at calculations: %s",
        run_started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    if not _wait_for_first_card(page, timeout_ms=action_timeout_ms):
        logger.info(
            "No applied-job cards visible after initial load; nothing to backfill."
        )
        return stats

    applied_file.parent.mkdir(parents=True, exist_ok=True)

    consecutive_failures = 0
    max_consecutive_failures = 3
    iteration = 0

    while True:
        iteration += 1

        if not _wait_for_first_card(page, timeout_ms=4_000):
            logger.info("No more applied-job cards; backfill finished.")
            break

        try:
            title, company, publish_text = _read_first_card(
                page, action_timeout_ms=action_timeout_ms
            )
        except PlaywrightError as exc:
            consecutive_failures += 1
            logger.warning(
                "Could not read first card on iteration %d (%s). "
                "Consecutive failures: %d/%d",
                iteration,
                exc,
                consecutive_failures,
                max_consecutive_failures,
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "Too many consecutive read failures; aborting backfill."
                )
                break
            time.sleep(0.8)
            continue

        consecutive_failures = 0

        offset = parse_publish_time(publish_text)
        if offset is None:
            logger.warning(
                "Iteration %d: unrecognised publish-time tag %r for %r @ %r; "
                "writing record with empty applied_at and continuing.",
                iteration,
                publish_text,
                title,
                company,
            )
            applied_at_iso = ""
            stats.unparsed_publish += 1
        else:
            if offset > stop_after:
                logger.info(
                    "Iteration %d: publish age %s exceeds threshold %s "
                    "(%r @ %r); stopping backfill.",
                    iteration,
                    _format_timedelta(offset),
                    _format_timedelta(stop_after),
                    title,
                    company,
                )
                break
            applied_at_iso = (run_started - offset).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        record = {
            "applied_at": applied_at_iso,
            "external_url": "",
            "source": source,
            "provider": provider,
            "company_slug": "",
            "company_name": company,
            "role_title": title,
            "submission_status": submission_status,
            "resume_used": "",
        }
        try:
            with applied_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error(
                "Could not append to %s (%s); aborting backfill.",
                applied_file,
                exc,
            )
            break

        stats.recorded += 1
        logger.info(
            "Recorded #%d: %r @ %r [%s -> applied_at=%s]",
            stats.recorded,
            title,
            company,
            publish_text,
            applied_at_iso or "<unparsed>",
        )

        # Click X on the same card we just logged. We capture the
        # title beforehand and confirm the first-card title rotates
        # afterwards so a missed click doesn't silently double-log
        # the same card on the next iteration.
        if not _click_first_remove(page, action_timeout_ms=action_timeout_ms):
            logger.error(
                "Could not click remove (X) button on iteration %d; aborting "
                "backfill so we don't double-log the same card.",
                iteration,
            )
            break
        stats.removed += 1

        if not _wait_for_card_removed(
            page,
            prior_title=title,
            timeout_ms=4_000,
        ):
            logger.warning(
                "First card title %r did not change after X click on "
                "iteration %d; bailing to avoid double-logging.",
                title,
                iteration,
            )
            break

    logger.info(
        "Backfill done. recorded=%d removed=%d unparsed_publish=%d "
        "(file=%s)",
        stats.recorded,
        stats.removed,
        stats.unparsed_publish,
        applied_file,
    )
    return stats


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------


def _wait_for_first_card(page: Page, *, timeout_ms: int) -> bool:
    """Block until at least one role-title ``<h2>`` is visible."""
    try:
        page.locator(selectors.APPLIED_JOB_TITLE).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _read_first_card(
    page: Page, *, action_timeout_ms: int
) -> tuple[str, str, str]:
    """Return ``(role_title, company_name, publish_time_text)``.

    Each field is independently the first matching node on the page;
    on the Applied feed the four card-level selectors stay in sync
    because every card surfaces exactly one of each. We deliberately
    avoid a "find the parent card container" walk because Ant Design's
    rotating ``css-*`` hashes make a stable container selector
    impossible to pin without the index_card-prefix we don't yet have.
    """
    short_timeout = min(action_timeout_ms, 6_000)
    title = (
        page.locator(selectors.APPLIED_JOB_TITLE)
        .first.inner_text(timeout=short_timeout)
        or ""
    ).strip()
    company = (
        page.locator(selectors.APPLIED_COMPANY_NAME)
        .first.inner_text(timeout=short_timeout)
        or ""
    ).strip()
    publish_text = (
        page.locator(selectors.APPLIED_PUBLISH_TIME)
        .first.inner_text(timeout=short_timeout)
        or ""
    ).strip()
    return title, company, publish_text


def _click_first_remove(page: Page, *, action_timeout_ms: int) -> bool:
    """Click the first card's X / remove-card button.

    Retries up to three times because the Ant card sometimes detaches
    mid-animation (the click handler races a row-shift transition).
    """
    short_timeout = min(action_timeout_ms, 6_000)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        button = page.locator(selectors.APPLIED_REMOVE_BUTTON).first
        try:
            button.scroll_into_view_if_needed(timeout=short_timeout)
            button.click(timeout=action_timeout_ms)
            return True
        except PlaywrightError as exc:
            last_error = exc
            logger.warning(
                "Remove (X) click attempt %d failed: %s", attempt, exc
            )
            time.sleep(0.6)
    if last_error is not None:
        logger.error("Giving up on remove (X) click: %s", last_error)
    return False


def _wait_for_card_removed(
    page: Page, *, prior_title: str, timeout_ms: int
) -> bool:
    """Wait until the first card's title is no longer ``prior_title``.

    Returns ``True`` once the feed shrinks (zero cards) or the new
    first-card title differs from ``prior_title``. Returns ``False``
    if neither condition is met within ``timeout_ms`` -- in that
    state the caller should bail rather than risk re-logging the
    same card.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            count = page.locator(selectors.APPLIED_JOB_TITLE).count()
        except PlaywrightError:
            count = 0
        if count == 0:
            return True
        try:
            current = (
                page.locator(selectors.APPLIED_JOB_TITLE)
                .first.inner_text(timeout=500)
                or ""
            ).strip()
        except PlaywrightError:
            current = ""
        if current and current != prior_title:
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _format_timedelta(td: timedelta) -> str:
    """Render ``td`` for log output ("3d 4h 12m" rather than 282720s)."""
    total = int(td.total_seconds())
    if total <= 0:
        return "0s"
    parts: list[str] = []
    days, total = divmod(total, 24 * 60 * 60)
    if days:
        parts.append(f"{days}d")
    hours, total = divmod(total, 60 * 60)
    if hours:
        parts.append(f"{hours}h")
    minutes, seconds = divmod(total, 60)
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"
