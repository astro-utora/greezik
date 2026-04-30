"""Thin wrapper that delegates BID/SKIP decisions to the vendored
``<project_root>/steps/step0_judge_jd.py``.

The full three-layer judge (title blacklist + soft-blacklist with dev
override, hard regex disqualifiers, weighted fit score against the
resume vocabulary) lives in the **vendored** ``steps/`` files copied
verbatim from the upstream bot. Greezik must stay byte-identical to
them; this wrapper exists only to:

* present a stable :func:`judge_jd(jd_text, index)` signature so
  :mod:`greezik.autobid` doesn't need to know about ``steps/``,
* arrange ``sys.path`` so step0's bare ``from step1_match_resume``
  import resolves (and so step1's bare ``from utils.paths import``
  resolves to the ``utils/`` package at the project root),
* keep the second positional ``index`` argument for compatibility
  (step0 builds its own vocabulary from
  ``<project_root>/steps/_resume_cache.json``, which
  :func:`greezik.resume_index.load_resume_index` keeps up to date).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .resume_index import ResumeIndex

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_STEPS_DIR = _PROJECT_ROOT / "steps"

# step0 does ``from step1_match_resume import ...`` (bare module name),
# so ``<root>/steps/`` must be on sys.path. step1 in turn does
# ``from utils.paths import resumes_dir`` after adding its own
# ``BASE_DIR`` (= the project root) to sys.path, so we pre-add the
# project root too.
for _p in (_STEPS_DIR, _PROJECT_ROOT):
    p_str = str(_p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)


def judge_jd(jd_text: str, index: ResumeIndex) -> tuple[bool, list[str], dict]:
    """Return ``(should_bid, reasons, details)`` for ``jd_text``.

    Delegates to ``step0_judge_jd.judge``. The ``index`` argument is
    accepted for ABI compatibility but unused here -- step0 reads its
    resume vocabulary from ``<project_root>/steps/_resume_cache.json``,
    which is written by
    :func:`greezik.resume_index.load_resume_index`.
    """
    import step0_judge_jd  # noqa: PLC0415  -- deferred until sys.path is ready

    should_bid, reasons, details = step0_judge_jd.judge(jd_text)
    return should_bid, reasons, details
