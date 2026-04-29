"""Shared resume corpus loader for Greezik.

The judge (:mod:`greezik.jd_judge`) and the matcher
(:mod:`greezik.resume_match`) both need the full text of every PDF in
``APPLICANT_RESUMES_DIR``. Re-extracting ~85 PDFs on every job would be
slow and wasteful, so this module builds a small JSON cache keyed on
``(filename, mtime, size)`` and exposes a single
:class:`ResumeIndex` whose ``entries`` list is consumed by both modules.

The cache lives at ``<project_root>/.cache/resume_index.json``. Delete
that file to force a full re-scan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_CACHE_FILENAME = "resume_index.json"
_DEFAULT_CACHE_DIR_NAME = ".cache"


@dataclass(frozen=True)
class ResumeEntry:
    """One resume PDF with its extracted text."""

    path: Path
    name: str
    mtime: int
    size: int
    text: str
    text_lower: str


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
    project_root: Path | None = None,
) -> ResumeIndex:
    """Return the :class:`ResumeIndex` for ``resumes_dir``.

    PDFs whose ``(name, mtime, size)`` are unchanged since the previous
    run are read from the JSON cache; new / modified PDFs are
    re-extracted with pypdf.
    """

    folder = resumes_dir.resolve()
    if not folder.exists():
        logger.warning("Resume folder %s does not exist; matcher disabled.", folder)
        return ResumeIndex(folder=folder, entries=[])

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        logger.warning("No PDFs found under %s; matcher disabled.", folder)
        return ResumeIndex(folder=folder, entries=[])

    cache_path = _resolve_cache_path(project_root)
    cache: dict[str, dict] = _load_cache(cache_path)

    fresh_cache: dict[str, dict] = {}
    entries: list[ResumeEntry] = []
    extracted = 0
    cache_hits = 0

    for pdf_path in pdfs:
        try:
            stat = pdf_path.stat()
        except OSError as exc:
            logger.warning("Cannot stat resume %s: %s", pdf_path, exc)
            continue
        mtime = int(stat.st_mtime)
        size = stat.st_size
        cached = cache.get(pdf_path.name)
        if cached and cached.get("mtime") == mtime and cached.get("size") == size:
            text = cached.get("text") or ""
            cache_hits += 1
        else:
            text = _extract_pdf_text(pdf_path)
            extracted += 1
        fresh_cache[pdf_path.name] = {"mtime": mtime, "size": size, "text": text}
        entries.append(
            ResumeEntry(
                path=pdf_path,
                name=pdf_path.name,
                mtime=mtime,
                size=size,
                text=text,
                text_lower=text.lower(),
            )
        )

    _save_cache(cache_path, fresh_cache)
    logger.info(
        "Resume index ready: %d PDF(s) (%d cached, %d extracted) from %s",
        len(entries),
        cache_hits,
        extracted,
        folder,
    )
    return ResumeIndex(folder=folder, entries=entries)


def _resolve_cache_path(project_root: Path | None) -> Path:
    root = (project_root or Path.cwd()).resolve()
    cache_dir = root / _DEFAULT_CACHE_DIR_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _CACHE_FILENAME


def _load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # corrupt / unreadable cache is non-fatal
        logger.warning("Resume cache %s unreadable (%s); will rebuild.", path, exc)
        return {}


def _save_cache(path: Path, data: dict[str, dict]) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write resume cache %s: %s", path, exc)


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning(
            "pypdf is not installed; resume PDFs cannot be parsed. "
            "Run `pip install pypdf` to enable per-job resume matching."
        )
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        logger.warning("Failed to open resume PDF %s: %s", path, exc)
        return ""

    chunks: list[str] = []
    for page_num, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            logger.debug("Skipped page %d of %s (extract failed: %s)", page_num, path.name, exc)
            continue
        text = text.strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)
