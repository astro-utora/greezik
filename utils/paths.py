"""Compatibility shim for ``step1_match_resume.py``'s
``from utils.paths import resumes_dir`` import.

The vendored ``steps/step1_match_resume.py`` was originally written
for the dice-auto-bidder project, where ``utils.paths`` lived beside
``steps/`` and resolved to that bot's ``resumes/`` folder. Greezik
exposes the same name here (at ``<project_root>/utils/paths.py``) so
the file can stay byte-identical to the upstream copy while reading
the directory we configure via ``APPLICANT_RESUMES_DIR`` in the .env.

Greezik never actually walks the filesystem through this function --
``greezik.resume_index`` already builds the corpus -- but step1's
module-level ``RESUMES_DIR = _resumes_dir()`` evaluates at import
time, so this shim must always return *some* :class:`~pathlib.Path`.
"""

from __future__ import annotations

import os
from pathlib import Path


def resumes_dir() -> Path:
    """Return the absolute path to the resume corpus directory.

    Reads ``APPLICANT_RESUMES_DIR`` from the environment so the value
    stays in sync with the rest of Greezik. Falls back to
    ``<project_root>/resumes`` so the import never fails just because
    the env var hasn't been loaded yet.
    """
    raw = os.environ.get("APPLICANT_RESUMES_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # ``utils/paths.py`` -> ``utils/`` -> ``<project_root>``.
    return (Path(__file__).resolve().parent.parent / "resumes").resolve()
