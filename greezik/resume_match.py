"""Thin wrapper that delegates per-job resume scoring to the vendored
``<project_root>/steps/step1_match_resume.py``.

The full scoring pipeline (title-weighted profile, IDF discrimination,
filename-tag bonus, critical-language penalty) lives in the
**vendored** ``steps/step1_match_resume.py`` copied verbatim from the
upstream bot. Greezik must stay byte-identical to it; this wrapper
exists only to:

* run step1's pure-function scoring against the in-memory
  :class:`~greezik.resume_index.ResumeIndex` Greezik already built (no
  extra disk reads / no second filesystem walk),
* present the stable :func:`match_best_resume(jd_text, index)`
  signature consumed by :mod:`greezik.autobid`.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from .resume_index import ResumeEntry, ResumeIndex

logger = logging.getLogger(__name__)


_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_STEPS_DIR = _PROJECT_ROOT / "steps"

# Make step1 importable via its bare module name and let it pick up
# the ``utils.paths`` shim from the project root.
for _p in (_STEPS_DIR, _PROJECT_ROOT):
    p_str = str(_p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)

# Importing step1 triggers its module-level setup (TECH_PHRASES,
# CATEGORY_MULTIPLIERS, etc.) which is exactly what we want.
import step1_match_resume as _step1  # noqa: E402, PLC0415


def detect_critical_languages(jd_text: str) -> set[str]:
    """Re-implements the language-criticality block from step1's
    :func:`find_best_resume` so we don't have to call step1's main
    (which walks the filesystem and prints scores).

    Mirrors the logic byte-for-byte; only difference is that we accept
    ``jd_text`` instead of reading ``Job_Description.txt``.
    """
    sections = _step1.parse_jd_sections(jd_text)
    critical: set[str] = set()
    req_text = sections.get("required", "")
    if req_text:
        for sentence in re.split(r"[.\n]", req_text):
            sent_lower = sentence.lower()
            if any(mod in sent_lower for mod in _step1.STRONG_MODIFIERS):
                for ph in _step1.extract_phrases_from_text(sentence):
                    if ph in _step1.PROGRAMMING_LANGUAGES:
                        critical.add(ph)
    collapsed: set[str] = set()
    seen: set[str] = set()
    for lang in critical:
        canonical = min([lang] + _step1.TECH_ALIASES.get(lang, []))
        if canonical not in seen:
            collapsed.add(canonical)
            seen.add(canonical)
            for alias in _step1.TECH_ALIASES.get(lang, []):
                seen.add(alias)
    return collapsed


def match_best_resume(
    jd_text: str,
    index: ResumeIndex,
) -> tuple[ResumeEntry | None, int, list[tuple[int, ResumeEntry]]]:
    """Return ``(best_entry, best_score, ranked)`` for ``jd_text``.

    The job profile is built with step1's :func:`build_job_profile`,
    IDF is computed with step1's :func:`compute_idf` over the in-memory
    DOCX texts, and each entry is scored with step1's
    :func:`score_resume`. ``ranked`` is the full sorted ``(score,
    entry)`` list; the index being empty yields ``(None, 0, [])``.
    """

    if not index.entries:
        return None, 0, []

    job_profile = _step1.build_job_profile(jd_text)
    critical = detect_critical_languages(jd_text)

    all_texts = [e.text for e in index.entries if e.text]
    idf = _step1.compute_idf(all_texts, list(job_profile.keys()))

    scored: list[tuple[int, ResumeEntry]] = []
    for entry in index.entries:
        if not entry.text:
            scored.append((0, entry))
            continue
        # step1 keys the filename-tag bonus off the resume's filename;
        # use the *uploaded* PDF name so identical-stem DOCX/PDF pairs
        # are scored the same way step1 would inside its own walk.
        sc = _step1.score_resume(
            entry.text, job_profile, entry.name, idf, critical
        )
        scored.append((sc, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_entry = scored[0]
    return best_entry, best_score, scored
