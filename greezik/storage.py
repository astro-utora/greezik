"""Persistence of applied / skipped / manual-required job URLs as JSON Lines.

Three append-only stores. The exact filenames are configurable via
``APPLIED_URLS_FILE`` / ``SKIPPED_URLS_FILE`` / ``MANUAL_REQUIRED_FILE``
in the .env; defaults live under ``logs/jobright/`` (which is git-ignored).

:class:`AppliedURLStore`
    Records each application Greezik (or the human, after a manual
    intervention) successfully completed. Default file:
    ``logs/jobright/big_log.jsonl``. Carries enough metadata
    (``company_name``, ``role_title``, ``applied_at``) for the
    per-company dedup window. The dedup index keys on a normalized
    form of ``company_name`` so e.g. ``"Higher Logic"``,
    ``"higher logic"``, and ``"Higher Logic, Inc."`` all collapse to
    the same employer.

:class:`SkippedURLStore`
    Records every job we deliberately did NOT apply to: non-Greenhouse
    portals, judge-skipped low-fit roles, recently-applied companies,
    submit failures the user dismissed, etc. Default file:
    ``logs/jobright/skipped_urls.jsonl``.

:class:`ManualRequiredStore`
    Records jobs whose Greenhouse submit didn't confirm AND the user
    has chosen NOT to be alerted about (``MANUAL_APPLY_ALERT=false``).
    Default file: ``logs/jobright/manual_required.jsonl`` so the
    human can replay these submissions in their own time without
    losing the failure detail.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


# Trailing tokens we drop when normalizing a company name so the
# per-company dedup treats e.g. ``"Twilio"``, ``"Twilio, Inc."``, and
# ``"Twilio Inc"`` as the same employer. Kept conservative -- only
# obvious legal-entity suffixes -- so unrelated companies that just
# happen to share a generic word never collide.
_COMPANY_SUFFIX_TOKENS = frozenset({
    "inc", "incorporated",
    "llc", "llp", "lp",
    "ltd", "limited",
    "corp", "corporation",
    "co", "company",
    "gmbh", "ag", "sa", "nv", "bv", "plc", "pte", "pty",
    "holdings", "group",
})


def _normalize_company_name(name: str) -> str:
    """Return a canonical key for ``name`` used by the dedup index.

    Lowercases, strips punctuation, collapses whitespace, then drops
    trailing legal-entity suffix tokens (``Inc``, ``LLC``,
    ``Holdings``, ...). Returns ``""`` when nothing remains so callers
    can short-circuit on empty inputs.

    Examples::

        "Speechify"            -> "speechify"
        "Higher Logic"         -> "higher logic"
        "CoLab"                -> "colab"
        "Twilio, Inc."         -> "twilio"
        "Acme Holdings Group"  -> "acme"
    """
    if not isinstance(name, str) or not name.strip():
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if not cleaned:
        return ""
    tokens = cleaned.split()
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


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
    ``normalized-company-name -> latest applied_at`` map so the apply
    loop can ask "did we apply to this company in the last N days?"
    without re-scanning the file every iteration. Names are
    normalized through :func:`_normalize_company_name` so casing,
    punctuation, and legal-entity suffixes (``Inc.``, ``LLC``, ...)
    don't break the match.
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
        # normalized-company-name -> most recent applied_at datetime (UTC).
        self._company_last_applied: dict[str, datetime] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def has(self, url: str) -> bool:
        return url in self._seen_urls

    def applied_to_company_within(self, company_name: str, days: int) -> bool:
        """True iff we applied to ``company_name`` within the last
        ``days`` days. ``days <= 0`` (or an empty / unrecognisable
        name) disables the check.
        """
        if days <= 0:
            return False
        key = _normalize_company_name(company_name)
        if not key:
            return False
        last = self._company_last_applied.get(key)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) <= timedelta(days=days)

    def last_applied_to_company(self, company_name: str) -> datetime | None:
        key = _normalize_company_name(company_name)
        if not key:
            return None
        return self._company_last_applied.get(key)

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
            name_key = _normalize_company_name(company_name)
            if name_key:
                prev = self._company_last_applied.get(name_key)
                if prev is None or now > prev:
                    self._company_last_applied[name_key] = now
            return True

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_older_than(self, days: int) -> int:
        """Drop records whose ``applied_at`` is older than ``days`` ago.

        Rewrites the JSONL file in-place via a temp-file + replace so a
        crash mid-prune leaves the original intact. Refreshes the
        in-memory ``_seen_urls`` and ``_company_last_applied`` indexes
        so subsequent dedup checks reflect the trimmed history.

        Returns the number of records removed. ``days <= 0`` is a
        no-op so callers can wire this to an env var without
        guarding the call.
        """
        if days <= 0 or not self.path.exists():
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        kept_records: list[dict] = []
        kept_lines: list[str] = []
        dropped = 0
        malformed = 0

        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            record = json.loads(stripped)
                        except json.JSONDecodeError:
                            malformed += 1
                            continue
                        when = _parse_iso_utc(record.get("applied_at") or "")
                        # Records without a timestamp (or unparseable
                        # ones) are kept -- we never want to silently
                        # discard anything we can't date.
                        if when is not None and when < cutoff:
                            dropped += 1
                            continue
                        kept_records.append(record)
                        kept_lines.append(stripped)
            except OSError as exc:
                logger.warning(
                    "Could not read %s for pruning: %s", self.path, exc
                )
                return 0

            if dropped == 0 and malformed == 0:
                return 0

            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    for line in kept_lines:
                        fh.write(line + "\n")
                tmp_path.replace(self.path)
            except OSError as exc:
                logger.warning(
                    "Could not rewrite %s during prune: %s", self.path, exc
                )
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                return 0

            self._seen_urls = set()
            self._company_last_applied = {}
            for record in kept_records:
                url = record.get("external_url")
                if isinstance(url, str):
                    self._seen_urls.add(url)
                name = record.get("company_name")
                when = _parse_iso_utc(record.get("applied_at") or "")
                name_key = _normalize_company_name(name) if isinstance(name, str) else ""
                if name_key and when is not None:
                    prev = self._company_last_applied.get(name_key)
                    if prev is None or when > prev:
                        self._company_last_applied[name_key] = when

        if dropped:
            logger.info(
                "Pruned %d applied-job record(s) older than %d day(s) from %s "
                "(%d remain, %d distinct companies).",
                dropped,
                days,
                self.path,
                len(self._seen_urls),
                len(self._company_last_applied),
            )
        if malformed:
            logger.info(
                "Dropped %d malformed line(s) while pruning %s.",
                malformed,
                self.path,
            )
        return dropped

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
                    name = record.get("company_name")
                    when = _parse_iso_utc(record.get("applied_at") or "")
                    name_key = (
                        _normalize_company_name(name)
                        if isinstance(name, str)
                        else ""
                    )
                    if name_key and when is not None:
                        prev = self._company_last_applied.get(name_key)
                        if prev is None or when > prev:
                            self._company_last_applied[name_key] = when
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


class ManualRequiredStore:
    """Append-only JSONL store for jobs whose autobid submit failed
    AND the user has opted out of the synchronous "manual apply"
    alert (``MANUAL_APPLY_ALERT=false``).

    Each record carries the failure ``status`` (``needs_code_failed``,
    ``form_error``, ``no_change``, ...), any visible field-validation
    errors, and the role/company metadata so the user can later
    replay the submission manually without losing the context.
    URL-level dedupe is applied so a re-run of the same job doesn't
    log a duplicate entry.
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
        status: str,
        error: str | None = None,
        submission_errors: list[str] | None = None,
        provider: str = "",
        company_slug: str = "",
        company_name: str = "",
        role_title: str = "",
        resume_used: str = "",
        head: str = "",
        extra: dict | None = None,
    ) -> bool:
        """Persist ``url`` as needing manual completion. Returns
        ``True`` if newly stored, ``False`` if already on file."""
        with self._lock:
            if url in self._seen_urls:
                return False
            record: dict = {
                "recorded_at": _now_iso_utc(),
                "external_url": url,
                "status": status or "unknown",
                "source": self.source,
            }
            if head:
                record["head"] = head
            if error:
                record["error"] = error
            if submission_errors:
                # Keep the list bounded so a runaway form-validator
                # doesn't blow up the JSONL file.
                record["submission_errors"] = list(submission_errors[:20])
            if provider:
                record["provider"] = provider
            if company_slug:
                record["company_slug"] = company_slug
            if company_name:
                record["company_name"] = company_name
            if role_title:
                record["role_title"] = role_title
            if resume_used:
                record["resume_used"] = resume_used
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
                "Loaded %d previously manual-required URL(s) from %s.",
                len(self._seen_urls),
                self.path,
            )
