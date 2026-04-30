"""Scrape the jobright.ai job-info detail panel.

The new apply flow drives the loop through jobright's own job list:

1. Click the first ``<h2 class="...index_job-title__...">`` on the feed.
2. Jobright opens a detail view (URL becomes ``/jobs/info/<id>``).
3. We pull the visible ``Title / Company / Company summary / Salary``
   plus the ``Responsibilities`` / ``Qualification`` sections off that
   detail view and persist them to two files in ``bid/jobright/``:

       Job_Description.txt   -- title + responsibilities + required +
                                preferred (the body the matcher / judge
                                actually score against).
       Company_Summary.txt   -- the company blurb shown above the JD,
                                fed into the AI prompt as extra
                                grounding so generated answers reflect
                                the employer's mission / domain.

4. The Greenhouse autobid step then reads both files (no longer the
   Greenhouse popup body) for the JD-judge, resume matcher, and
   AI-answer prompts.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from . import selectors

logger = logging.getLogger(__name__)


# Where the per-job artifacts (JD text + chosen resume PDF) are written.
# A relative ``bid/jobright`` folder under the project root keeps the
# layout stable regardless of where Greezik is invoked from.
BID_OUTPUT_DIR = Path("bid") / "jobright"
JOB_DESCRIPTION_FILENAME = "Job_Description.txt"
COMPANY_SUMMARY_FILENAME = "Company_Summary.txt"
RESUME_COPY_FILENAME = "resume.pdf"


@dataclass
class JobrightJobDetails:
    """Structured snapshot of a single jobright detail panel.

    All fields are optional -- the page rarely renders every section,
    and missing values just produce shorter ``Job_Description.txt``
    files. ``title`` and ``company`` are the ones the apply log relies
    on, so callers should at least check those before writing the JD.
    """

    title: str = ""
    company: str = ""
    company_summary: str = ""
    salary: str = ""
    responsibilities: list[str] = field(default_factory=list)
    qualification_required: list[str] = field(default_factory=list)
    qualification_preferred: list[str] = field(default_factory=list)
    # Free-text "Qualification" tags (Java, React, ...) shown above the
    # Required / Preferred lists. Captured for completeness even though
    # we don't currently emit them into ``Job_Description.txt``.
    qualification_skills: list[str] = field(default_factory=list)

    def to_description_text(self) -> str:
        """Render the role-specific JD blob (no company summary).

        Layout::

            <Title>

            Job Details
            Responsibilities
            <bullet 1>
            ...

            Required
            <bullet 1>
            ...
            Preferred
            <bullet 1>
            ...

        Note the asymmetric blank line: there's a gap between the
        Responsibilities bullets and "Required", but the "Preferred"
        header sits immediately under the last "Required" bullet.

        The company summary is intentionally NOT included here -- it
        is written separately to ``Company_Summary.txt`` and fed
        into the AI prompt on its own track. See
        :meth:`to_company_summary_text`.
        """
        parts: list[str] = []

        if self.title:
            parts.append(self.title.strip())

        body: list[str] = []
        if self.responsibilities:
            body.append("Job Details")
            body.append("Responsibilities")
            body.extend(self.responsibilities)
        if self.qualification_required:
            if body:
                body.append("")
            body.append("Required")
            body.extend(self.qualification_required)
        if self.qualification_preferred:
            # No blank line between Required and Preferred -- matches
            # the spec exactly. If Required is missing entirely we still
            # leave the gap between the Responsibilities block and
            # Preferred so the file reads sensibly.
            if body and not self.qualification_required:
                body.append("")
            body.append("Preferred")
            body.extend(self.qualification_preferred)

        if body:
            parts.append("\n".join(body))

        return "\n\n".join(parts).strip() + ("\n" if parts else "")

    def to_company_summary_text(self) -> str:
        """Return the company blurb as written to ``Company_Summary.txt``.

        Empty string when the detail panel didn't render a summary;
        callers can use that to skip the extra prompt block.
        """
        return (self.company_summary or "").strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def wait_for_detail_panel(page: Page, *, timeout_ms: int) -> bool:
    """Wait until the jobright detail panel is rendered for the active job.

    The detail panel always shows an ``<h1 class="...index_job-title__...">``
    header (different hash than the list ``<h2>``) plus a "Job Details"
    section heading. We treat the H1 as the readiness signal because the
    sectioned content lazy-loads a moment after the URL changes.
    """

    try:
        page.locator(selectors.JOB_INFO_TITLE).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except PlaywrightTimeoutError:
        return False
    except PlaywrightError:
        return False


def extract_job_details(page: Page) -> JobrightJobDetails:
    """Pull every field we care about off the currently-rendered detail panel.

    Designed to be tolerant of partially-rendered DOM: any selector that
    fails to resolve just produces an empty string / list, and the caller
    can decide whether the resulting ``JobrightJobDetails`` is usable.
    """

    details = JobrightJobDetails(
        title=_text(page, selectors.JOB_INFO_TITLE),
        company=_text(page, selectors.JOB_INFO_COMPANY_NAME),
        company_summary=_text(page, selectors.JOB_INFO_COMPANY_SUMMARY),
        salary=_extract_salary(page),
    )

    sections = _collect_sections(page)
    details.responsibilities = sections.get("responsibilities", [])
    details.qualification_skills = sections.get("qualification_skills", [])
    details.qualification_required = sections.get("qualification_required", [])
    details.qualification_preferred = sections.get("qualification_preferred", [])

    logger.info(
        "Extracted job details: title=%r company=%r salary=%r "
        "(%d responsibility / %d required / %d preferred bullets)",
        details.title,
        details.company,
        details.salary,
        len(details.responsibilities),
        len(details.qualification_required),
        len(details.qualification_preferred),
    )
    return details


def write_job_description_file(
    details: JobrightJobDetails,
    *,
    project_root: Path,
) -> Path:
    """Persist ``details`` to ``<project_root>/bid/jobright/Job_Description.txt``.

    The directory is created on demand. Returns the resolved file path so
    callers can pass it straight to the resume matcher / AI answerer.
    """

    out_dir = (project_root / BID_OUTPUT_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / JOB_DESCRIPTION_FILENAME
    out_path.write_text(details.to_description_text(), encoding="utf-8")
    logger.info("Saved jobright JD to %s", out_path)
    return out_path


def write_company_summary_file(
    details: JobrightJobDetails,
    *,
    project_root: Path,
) -> Path:
    """Persist the company summary to ``bid/jobright/Company_Summary.txt``.

    Always writes the file (creating an empty one when the detail
    panel didn't render a summary) so downstream code can rely on the
    path existing. The directory is created on demand.
    """

    out_dir = (project_root / BID_OUTPUT_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / COMPANY_SUMMARY_FILENAME
    summary = details.to_company_summary_text()
    out_path.write_text(summary + ("\n" if summary else ""), encoding="utf-8")
    if summary:
        logger.info(
            "Saved company summary (%d chars) to %s", len(summary), out_path
        )
    else:
        logger.info("No company summary on detail panel; wrote empty %s", out_path)
    return out_path


def copy_resume_to_bid_dir(
    resume_path: Path,
    *,
    project_root: Path,
) -> Path:
    """Copy ``resume_path`` to ``<project_root>/bid/jobright/resume.pdf``.

    The Greenhouse autobid uploads this staged copy (instead of the
    original PDF picked by the per-job matcher) so a single,
    well-known path is always used for the active job. Returns the
    destination path.
    """

    out_dir = (project_root / BID_OUTPUT_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / RESUME_COPY_FILENAME
    shutil.copyfile(resume_path, dest)
    logger.info("Copied resume %s -> %s", resume_path.name, dest)
    return dest


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _text(page: Page, selector: str) -> str:
    """First-match ``inner_text`` for ``selector``, normalised to one line."""
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return ""
        raw = loc.inner_text(timeout=2_000) or ""
    except PlaywrightError:
        return ""
    return " ".join(raw.split()).strip()


def _extract_salary(page: Page) -> str:
    """Find the ``$x/yr - $y/yr`` value on the metadata strip, if any.

    The salary item is one of several ``index_job-metadata-item__`` rows
    (Location, Job type, Posted ago, Salary). We disambiguate by looking
    for the ``money`` icon image and reading the sibling ``<span>``.
    Returns an empty string when the page omits a salary.
    """

    try:
        items = page.locator(selectors.JOB_METADATA_ITEM)
        count = items.count()
    except PlaywrightError:
        return ""

    for i in range(count):
        item = items.nth(i)
        try:
            icon = item.locator("img").first
            if icon.count() == 0:
                continue
            alt = (icon.get_attribute("alt") or "").lower()
            src = (icon.get_attribute("src") or "").lower()
        except PlaywrightError:
            continue
        if alt == "money" or "money" in src:
            try:
                span = item.locator("span").first
                if span.count() == 0:
                    continue
                value = (span.inner_text(timeout=1_500) or "").strip()
            except PlaywrightError:
                continue
            return " ".join(value.split())
    return ""


def _collect_sections(page: Page) -> dict[str, list[str]]:
    """Walk the ``index_sectionContent__`` blocks for the labels we want.

    Each section starts with an ``<h2 class="...index_label__...">``
    heading whose text identifies the section ("Responsibilities" or
    "Qualification"). Inside ``Qualification`` we further split into the
    "Required" and "Preferred" sub-lists by looking for
    ``<h4 class="...index_qualifications-sub-title__...">``.
    """

    out: dict[str, list[str]] = {
        "responsibilities": [],
        "qualification_skills": [],
        "qualification_required": [],
        "qualification_preferred": [],
    }

    try:
        sections = page.locator(selectors.JOB_INFO_SECTION)
        count = sections.count()
    except PlaywrightError:
        return out

    for i in range(count):
        section = sections.nth(i)
        try:
            label_el = section.locator(selectors.JOB_INFO_SECTION_LABEL).first
            if label_el.count() == 0:
                continue
            label = (label_el.inner_text(timeout=1_500) or "").strip().lower()
        except PlaywrightError:
            continue

        if label.startswith("responsibilities"):
            out["responsibilities"] = _read_bullets(section)
        elif label.startswith("qualification"):
            out["qualification_skills"] = _read_qualification_skills(section)
            req, pref = _read_qualification_subsections(section)
            out["qualification_required"] = req
            out["qualification_preferred"] = pref

    return out


def _read_bullets(scope: Locator) -> list[str]:
    """Return non-empty bullet text from ``<span class="...index_listText__...">``."""
    bullets: list[str] = []
    try:
        spans = scope.locator(selectors.JOB_INFO_LIST_TEXT)
        count = spans.count()
    except PlaywrightError:
        return bullets
    for i in range(count):
        try:
            text = (spans.nth(i).inner_text(timeout=800) or "").strip()
        except PlaywrightError:
            continue
        if text:
            bullets.append(" ".join(text.split()))
    return bullets


def _read_qualification_skills(scope: Locator) -> list[str]:
    """Return the free-text qualification tags ("Java", "React", ...)."""
    tags: list[str] = []
    try:
        tag_locator = scope.locator(selectors.JOB_INFO_QUALIFICATION_TAG)
        count = tag_locator.count()
    except PlaywrightError:
        return tags
    for i in range(count):
        try:
            text = (tag_locator.nth(i).inner_text(timeout=800) or "").strip()
        except PlaywrightError:
            continue
        if text:
            tags.append(" ".join(text.split()))
    return tags


def _read_qualification_subsections(scope: Locator) -> tuple[list[str], list[str]]:
    """Split the qualification block into ``(required, preferred)`` bullets.

    The page renders Required/Preferred as separate ``<h4>`` headings,
    each followed by sibling ``index_text-row__`` rows. There's no
    structural wrapper so we can't ``locator()`` per heading; instead
    we walk the heading + bullet DOM in document order via
    ``page.evaluate``.
    """

    try:
        return scope.evaluate(_QUALIFICATION_SUBSECTIONS_JS)
    except PlaywrightError:
        return [], []


# Returns ``[required[], preferred[]]`` for a qualification ``<section>``.
# Document-order traversal so we can attribute each bullet to the most
# recent ``<h4>`` heading we passed.
_QUALIFICATION_SUBSECTIONS_JS = """
(section) => {
  const required = [];
  const preferred = [];
  let current = null;

  const walker = document.createTreeWalker(
    section,
    NodeFilter.SHOW_ELEMENT
  );

  while (walker.nextNode()) {
    const el = walker.currentNode;
    const tag = el.tagName;
    if (tag === 'H4') {
      const t = (el.innerText || '').trim().toLowerCase();
      if (t.startsWith('required')) {
        current = required;
      } else if (t.startsWith('preferred')) {
        current = preferred;
      } else {
        current = null;
      }
      continue;
    }
    if (current === null) continue;
    if (
      el.tagName === 'SPAN'
      && (el.className || '').toString().includes('index_listText__')
    ) {
      const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (txt) current.push(txt);
    }
  }
  return [required, preferred];
}
"""


def settle_after_url_change(page: Page, *, seconds: float = 0.6) -> None:
    """Tiny wait helper so callers don't repeat ``time.sleep`` boilerplate."""
    time.sleep(max(0.0, seconds))
