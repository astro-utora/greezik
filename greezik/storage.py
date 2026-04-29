"""Persistence of applied job URLs as JSON Lines."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class AppliedURLStore:
    """Append-only JSONL store with in-memory dedupe."""

    def __init__(self, path: Path, source: str = "jobright.ai/jobs/recommend") -> None:
        self.path = path
        self.source = source
        self._lock = Lock()
        self._seen: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

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
                        logger.warning("Skipping malformed JSONL line in %s", self.path)
                        continue
                    url = record.get("external_url")
                    if isinstance(url, str):
                        self._seen.add(url)
        except OSError as exc:
            logger.warning("Could not read existing %s: %s", self.path, exc)

        if self._seen:
            logger.info("Loaded %d previously applied URLs from %s", len(self._seen), self.path)

    def has(self, url: str) -> bool:
        return url in self._seen

    def add(self, url: str, *, extra: dict | None = None) -> bool:
        """Persist ``url``. Returns ``True`` if newly stored, ``False`` if duplicate."""

        with self._lock:
            if url in self._seen:
                return False
            record = {
                "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "external_url": url,
                "source": self.source,
            }
            if extra:
                record.update(extra)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._seen.add(url)
            return True

    def __len__(self) -> int:
        return len(self._seen)
