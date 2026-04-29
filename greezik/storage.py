"""Persistence of applied / skipped job URLs as JSON Lines.

Two append-only stores:

:class:`AppliedURLStore`
    Records each application Greezik (or the human, after a manual
    intervention) successfully completed. Backs ``applied_jobs.jsonl``.
    Carries enough metadata (``company_slug``, ``role_title``,
    ``applied_at``) for the per-company dedup window.

:class:`SkippedURLStore`
    Records every job we deliberately did NOT apply to: non-Greenhouse
    portals, judge-skipped low-fit roles, recently-applied companies,
    submit failures the user dismissed, etc. Backs
    ``skipped_jobs.jsonl``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse a ``YYYY-mm-ddTHH:MM:SSZ`` (or naive ISO) timestamp.

    Returns ``None`` for unparseable / missing input. Tolerates a
    trailing ``Z`` and bare-naive timestamps for backwards compat with
    older records.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class AppliedURLStore:
    """Append-only JSONL store of successfully-applied job URLs.

    In addition to the URL set, the store maintains a parallel
    ``slug -> latest applied_at`` map so the apply loop can ask
    "did we apply to this company in the last N days?" without
    re-scanning the file every iteration.
    """

    def __init__(
        self,
        path: Path,
        source: str = "jobright.ai/jobs/recommend",
    ) -> None:
        self.path = path
        self.source = source
        self._lock = Lock()
        self._seen_urls: set[str] = set()
        # slug -> most recent applied_at datetime (UTC).
        self._company_last_applied: dict[str, datetime] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def has(self, url: str) -> bool:
        return url in self._seen_urls

    def applied_to_company_within(self, company_slug: str, days: int) -> bool:
        """True iff we applied to ``company_slug`` within the last
        ``days`` days. ``days <= 0`` disables the check."""
        if days <= 0 or not company_slug:
            return False
        last = self._company_last_applied.get(company_slug.lower())
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) <= timedelta(days=days)

    def last_applied_to_company(self, company_slug: str) -> datetime | None:
        if not company_slug:
            return None
        return self._company_last_applied.get(company_slug.lower())

    def __len__(self) -> int:
        return len(self._seen_urls)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        company_slug: str = "",
        company_name: str = "",
        role_title: str = "",
        provider: str = "",
        submission_status: str = "",
        resume_used: str = "",
        extra: dict | None = None,
    ) -> bool:
        """Persist ``url`` as applied. Returns ``True`` if newly
        stored, ``False`` if already on file (URL-level dedupe)."""

        with self._lock:
            if url in self._seen_urls:
                return False
            now = datetime.now(timezone.utc)
            record: dict = {
                "applied_at": _now_iso_utc(),
                "external_url": url,
                "source": self.source,
            }
            if provider:
                record["provider"] = provider
            if company_slug:
                record["company_slug"] = company_slug
            if company_name:
                record["company_name"] = company_name
            if role_title:
                record["role_title"] = role_title
            if submission_status:
                record["submission_status"] = submission_status
            if resume_used:
                record["resume_used"] = resume_used
            if extra:
                record.update(extra)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._seen_urls.add(url)
            if company_slug:
                slug_key = company_slug.lower()
                prev = self._company_last_applied.get(slug_key)
                if prev is None or now > prev:
                    self._company_last_applied[slug_key] = now
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed JSONL line in %s", self.path
                        )
                        continue
                    url = record.get("external_url")
                    if isinstance(url, str):
                        self._seen_urls.add(url)
                    slug = record.get("company_slug")
                    when = _parse_iso_utc(record.get("applied_at") or "")
                    if isinstance(slug, str) and slug and when is not None:
                        slug_key = slug.lower()
                        prev = self._company_last_applied.get(slug_key)
                        if prev is None or when > prev:
                            self._company_last_applied[slug_key] = when
        except OSError as exc:
            logger.warning("Could not read existing %s: %s", self.path, exc)

        if self._seen_urls:
            logger.info(
                "Loaded %d previously applied URL(s) from %s "
                "(%d distinct companies tracked).",
                len(self._seen_urls),
                self.path,
                len(self._company_last_applied),
            )


class SkippedURLStore:
    """Append-only JSONL store for jobs we deliberately skipped.

    Each record carries a ``skip_reason`` string so the user can see
    *why* the bot bailed (judge SKIP, recent application, non-Greenhouse
    portal, manual cancel after submit failure, etc.). URL-level
    dedupe is still applied so the same URL isn't logged twice on a
    rerun.
    """

    def __init__(
        self,
        path: Path,
        source: str = "jobright.ai/jobs/recommend",
    ) -> None:
        self.path = path
        self.source = source
        self._lock = Lock()
        self._seen_urls: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def has(self, url: str) -> bool:
        return url in self._seen_urls

    def __len__(self) -> int:
        return len(self._seen_urls)

    def add(
        self,
        url: str,
        *,
        skip_reason: str,
        provider: str = "",
        company_slug: str = "",
        company_name: str = "",
        role_title: str = "",
        extra: dict | None = None,
    ) -> bool:
        """Persist ``url`` as skipped. Returns ``True`` if newly
        stored, ``False`` if the same URL was already recorded."""
        with self._lock:
            if url in self._seen_urls:
                return False
            record: dict = {
                "skipped_at": _now_iso_utc(),
                "external_url": url,
                "skip_reason": skip_reason or "unspecified",
                "source": self.source,
            }
            if provider:
                record["provider"] = provider
            if company_slug:
                record["company_slug"] = company_slug
            if company_name:
                record["company_name"] = company_name
            if role_title:
                record["role_title"] = role_title
            if extra:
                record.update(extra)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._seen_urls.add(url)
            return True

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed JSONL line in %s", self.path
                        )
                        continue
                    url = record.get("external_url")
                    if isinstance(url, str):
                        self._seen_urls.add(url)
        except OSError as exc:
            logger.warning("Could not read existing %s: %s", self.path, exc)

        if self._seen_urls:
            logger.info(
                "Loaded %d previously skipped URL(s) from %s.",
                len(self._seen_urls),
                self.path,
            )
