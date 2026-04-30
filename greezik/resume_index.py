"""Shared resume corpus loader for Greezik.

The judge (:mod:`greezik.jd_judge`) and the matcher
(:mod:`greezik.resume_match`) both need the full text of every resume
in ``APPLICANT_RESUMES_DIR``.

The folder is expected to contain *paired* ``.docx`` + ``.pdf`` files
with identical stems (e.g.
``Tyler_Jung_Sr_Software_Engineer(.NET,Angular,Azure).docx`` and the
matching ``...(.NET,Angular,Azure).pdf``). Greezik scores each
candidate using the **DOCX** body -- which preserves clean paragraphs
without the line-wrap artefacts pypdf produces -- and uploads the
**PDF** with the same stem when that resume is picked. DOCX files
without a paired PDF are skipped with a warning, since we cannot
deliver them to Greenhouse.

Cache is owned by the vendored ``step1_match_resume`` module: it
lives at ``<project_root>/steps/_resume_cache.json`` in step1's exact
schema (``{filename: {"mtime": str, "text": str}}``) and is
read/written through step1's :func:`load_cache`, :func:`save_cache`,
and :func:`get_resume_text` helpers. Sharing the cache between
Greezik and the vendored ``step0_judge_jd.py`` /
``step1_match_resume.py`` guarantees the judge's tech-phrase
vocabulary is built from exactly the same DOCX text the matcher
scores against. Delete the cache file to force a full re-scan.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_STEPS_DIR = _PROJECT_ROOT / "steps"

# The vendored ``steps/`` files import each other by bare module name
# (``from step1_match_resume import ...``), and step1 in turn does
# ``from utils.paths import resumes_dir`` after putting its own
# ``BASE_DIR`` (the project root) on sys.path. Pre-add both so the
# import below resolves regardless of the current working dir.
for _p in (_STEPS_DIR, _PROJECT_ROOT):
    _p_str = str(_p)
    if _p_str not in sys.path:
        sys.path.insert(0, _p_str)

import step1_match_resume as _step1  # noqa: E402  -- needs sys.path setup above


@dataclass(frozen=True)
class ResumeEntry:
    """One resume in the corpus.

    * :attr:`path` -- absolute path of the *PDF* that will be uploaded
      to Greenhouse when this entry is selected as the best match.
    * :attr:`name` -- PDF filename (used for log lines + the
      filename-tag bonus inside :func:`score_resume`).
    * :attr:`source_path` / :attr:`source_name` -- the DOCX whose body
      text was used to score this entry.
    * :attr:`mtime` / :attr:`size` -- DOCX file metadata, kept for
      diagnostics. Cache invalidation is owned by step1 and keys off
      ``str(mtime)``.
    * :attr:`text` / :attr:`text_lower` -- extracted DOCX body text.
    """

    path: Path
    name: str
    mtime: int
    size: int
    text: str
    text_lower: str
    source_path: Path
    source_name: str


@dataclass
class ResumeIndex:
    """Container for every resume in ``APPLICANT_RESUMES_DIR``."""

    folder: Path
    entries: list[ResumeEntry]

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def load_resume_index(
    resumes_dir: Path,
    *,
    project_root: Path | None = None,  # noqa: ARG001  -- kept for ABI compat
) -> ResumeIndex:
    """Return the :class:`ResumeIndex` for ``resumes_dir``.

    Scoring uses the body text of the ``.docx`` files in the folder;
    the entry's :attr:`ResumeEntry.path` points at the same-stem
    ``.pdf`` for upload. Cache hits / misses are decided by the
    vendored :func:`step1_match_resume.get_resume_text`, which keys on
    ``(filename, str(mtime))`` and stores the result in
    ``<project_root>/steps/_resume_cache.json``. DOCX files without a
    paired PDF are skipped (they can't be uploaded to Greenhouse).
    """

    folder = resumes_dir.resolve()
    if not folder.exists():
        logger.warning("Resume folder %s does not exist; matcher disabled.", folder)
        return ResumeIndex(folder=folder, entries=[])

    docxs = sorted(folder.glob("*.docx"))
    if not docxs:
        logger.warning("No DOCX resumes found under %s; matcher disabled.", folder)
        return ResumeIndex(folder=folder, entries=[])

    cache = _step1.load_cache()

    entries: list[ResumeEntry] = []
    cache_hits = 0
    extracted = 0
    skipped_no_pdf = 0
    skipped_errors = 0

    for docx_path in docxs:
        pdf_path = docx_path.with_suffix(".pdf")
        if not pdf_path.exists():
            logger.warning(
                "Skipping %s: no paired PDF (%s) found for upload.",
                docx_path.name,
                pdf_path.name,
            )
            skipped_no_pdf += 1
            continue
        try:
            stat = docx_path.stat()
        except OSError as exc:
            logger.warning("Cannot stat resume %s: %s", docx_path, exc)
            skipped_errors += 1
            continue

        # Decide hit/miss BEFORE delegating to step1 so we can report
        # cache stats. step1 keys on ``str(stat.st_mtime)``.
        mtime_str = str(stat.st_mtime)
        cached = cache.get(docx_path.name)
        is_hit = bool(cached) and cached.get("mtime") == mtime_str

        try:
            text = _step1.get_resume_text(docx_path, cache)
        except Exception as exc:  # noqa: BLE001  -- one bad doc shouldn't abort the run
            logger.warning("Failed to read resume DOCX %s: %s", docx_path, exc)
            skipped_errors += 1
            continue

        if is_hit:
            cache_hits += 1
        else:
            extracted += 1

        entries.append(
            ResumeEntry(
                path=pdf_path,
                name=pdf_path.name,
                mtime=int(stat.st_mtime),
                size=stat.st_size,
                text=text,
                text_lower=text.lower(),
                source_path=docx_path,
                source_name=docx_path.name,
            )
        )

    try:
        _step1.save_cache(cache)
    except OSError as exc:
        logger.warning("Could not write resume cache: %s", exc)

    if skipped_no_pdf:
        logger.warning(
            "Skipped %d DOCX file(s) without a paired PDF in %s",
            skipped_no_pdf,
            folder,
        )
    if skipped_errors:
        logger.warning(
            "Skipped %d DOCX file(s) due to read errors in %s",
            skipped_errors,
            folder,
        )
    logger.info(
        "Resume index ready: %d resume(s) (%d cached, %d extracted) "
        "scored from DOCX, uploaded as PDF, from %s",
        len(entries),
        cache_hits,
        extracted,
        folder,
    )
    return ResumeIndex(folder=folder, entries=entries)
