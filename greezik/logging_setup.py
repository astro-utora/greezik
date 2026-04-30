"""Logging setup for Greezik.

Console-only. The ``logs/`` tree on disk is reserved for per-run JSONL
data files under ``logs/jobright/`` (applied / skipped /
manual-required); we deliberately do NOT write a rotating
``greezik.log`` text file. Long-form post-mortems should rely on the
shell/terminal capture or be saved out by the user explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(
    log_dir: Path | None = None,  # noqa: ARG001  -- kept for ABI compatibility
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure the root logger with a single console handler.

    Returns the root logger for convenience. Calling this more than
    once is safe; existing handlers are removed first so reruns don't
    accumulate them. The ``log_dir`` parameter is accepted for
    backwards compatibility with older callers but is ignored -- file
    logging is intentionally disabled.
    """

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return root
