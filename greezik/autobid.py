"""Autobid / autofill module.

Currently supports Greenhouse application forms (``job-boards.greenhouse.io``).
The entrypoint :func:`autobid_apply` decides which provider-specific filler to
use based on the page URL.

The Greenhouse filler walks the form once, classifies each field by its
visible label / aria-label / id, and either:

* fills it from the ApplicantProfile, OR
* asks the AI for an answer (text/textarea + dropdown options), OR
* logs a warning and skips it.

When ``submit=False`` (the default for the test CLI) the form is filled but
**not** submitted, so you can review the result manually.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from .ai_answer import AIAnswerer, BatchItem, QuestionContext, iter_options
from .jd_judge import judge_jd
from .profile import ApplicantProfile, _extract_resume_text
from .resume_index import ResumeIndex, load_resume_index
from .resume_match import match_best_resume

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class _FilledField:
    """Internal record of a successfully-filled field, used to verify the
    value sticks after subsequent fields are interacted with."""

    label: str
    selector: str          # CSS selector to relocate the element
    value: str             # what we filled it with
    kind: str              # "text" or "select"


@dataclass
class AutobidResult:
    provider: str
    url: str
    # Job metadata extracted from the application page. Populated as
    # early as possible (before judge / fill) so the dedup and
    # ``applied_jobs.jsonl`` bookkeeping can use them even on jobs
    # that are skipped or that fail mid-fill.
    role_title: str = ""
    company_slug: str = ""
    company_name: str = ""
    filled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    refilled: list[str] = field(default_factory=list)
    submitted: bool = False
    error: str | None = None
    # Submission outcome detail. One of:
    #   "not_attempted"   -- ``submit=False`` (dry run).
    #   "confirmed"       -- form submit redirected to /confirmation.
    #   "verified"        -- email verification code accepted, then confirmed.
    #   "needs_code_failed" -- code prompt appeared but couldn't be cleared.
    #   "form_error"      -- visible field errors after submit click.
    #   "no_change"       -- nothing happened (timeout, possibly hidden error).
    submission_status: str = "not_attempted"
    submission_errors: list[str] = field(default_factory=list)
    # Set when the JD judge decides this job is not worth bidding on
    # (manager / security / hardware / clearance-only / low fit, etc.).
    # The autobid returns *immediately* in that case -- no resume
    # upload, no form fill -- so the test runner can move on without
    # popping the manual review prompt.
    skip_reason: str | None = None
    judge_details: dict | None = None
    # Path of the resume that was actually uploaded for this job.
    # Differs from ``profile.resume_path`` when the per-job matcher
    # picked a better-fitting PDF from ``APPLICANT_RESUMES_DIR``.
    resume_used: Path | None = None
    # Local path where the captured JD was saved (for record-keeping
    # and for re-running the judge / matcher offline).
    jd_path: Path | None = None
    _tracker: list[_FilledField] = field(default_factory=list, repr=False)
    # Combobox ids that completed via the *free-form* fallback (typed
    # text accepted in lieu of a real listbox option). Tracked across
    # multi-pass `_fill_select_fields` invocations so we don't keep
    # retrying -- and ultimately failing -- the same unresponsive
    # autocomplete (e.g. Greenhouse's candidate-location field) on the
    # lazy-rendered second pass.
    _freeform_combo_ids: set[str] = field(default_factory=set, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None


def autobid_apply(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    *,
    submit: bool = False,
    action_timeout_ms: int = 15_000,
) -> AutobidResult:
    """Run the right autobid flow for the current ``page`` URL."""

    url = page.url
    if "job-boards.greenhouse.io" in url:
        return _fill_greenhouse(
            page,
            profile,
            ai,
            submit=submit,
            action_timeout_ms=action_timeout_ms,
        )
    return AutobidResult(
        provider="unknown",
        url=url,
        error=f"No autobid filler implemented for URL: {url}",
    )


# ---------------------------------------------------------------------------
# Resume index cache (loaded lazily on first use; reused across jobs)
# ---------------------------------------------------------------------------


_RESUME_INDEX_CACHE: dict[str, ResumeIndex] = {}


def _get_resume_index(resumes_dir: Path) -> ResumeIndex:
    """Return a memoized :class:`ResumeIndex` for ``resumes_dir``.

    The index is shared across every job in a single run so the 80+ PDF
    extraction (and IDF prep) only happens once per process.
    """

    key = str(resumes_dir.resolve())
    cached = _RESUME_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index = load_resume_index(resumes_dir)
    _RESUME_INDEX_CACHE[key] = index
    return index


# ---------------------------------------------------------------------------
# Job description extraction
# ---------------------------------------------------------------------------


# Cap so the JD doesn't dominate the prompt. Most Greenhouse JDs are
# 1-3k chars; rare ones balloon to 8k+. We keep up to ~6k characters,
# which is enough to capture the role / requirements / company values
# without dwarfing the resume context.
_JOB_DESCRIPTION_MAX_CHARS = 6_000

# Greenhouse application pages always render an "Apply for this job"
# heading above the form. Everything before that heading is the job
# description; everything after is the form's own labels and helper
# text (which we don't want polluting the AI prompt).
_GREENHOUSE_JD_END_MARKERS = (
    "Apply for this job",
    "Apply now",
    "Submit application",
)


# ---------------------------------------------------------------------------
# Role / company metadata extraction
# ---------------------------------------------------------------------------


# Greenhouse embed URLs encode the company as a stable slug in the
# ``for=`` query parameter (e.g. ``cordial81``, ``humanagency``,
# ``paradigminccareersopenpositions``). The slug is the most reliable
# dedup key because the slug is stable per company across all their
# open roles, even when the human-readable name varies between
# postings ("Cordial Software" vs "Cordial").
def extract_company_slug_from_url(url: str) -> str:
    """Public helper: pull the Greenhouse company slug from a
    ``job-boards.greenhouse.io`` apply URL.

    Used by the live runner (``applier.py``) to make the per-company
    dedup decision BEFORE calling :func:`autobid_apply`, so a job we
    already applied to within the dedup window is short-circuited
    before the form even loads.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    qs = parse_qs(parts.query or "")
    val = (qs.get("for") or [""])[0]
    return val.strip().lower()


# Backwards-compatible internal alias (was named with leading underscore
# before the helper had to be exposed to ``applier.py``).
_extract_company_slug_from_url = extract_company_slug_from_url


# Up to ~6 lines from the top of the visible JD that we'll consider
# when guessing a human-readable company name. Greenhouse embeds
# typically render the role on the first content line, the location
# on the second, and "Apply" / a CTA on the third -- so the company
# name (if present) is usually nearby.
_COMPANY_NAME_HINTS = re.compile(
    r"^\s*(?:about|join|welcome\s+to|at)\s+([A-Z][A-Za-z0-9 &.,'\-]{2,80})",
    re.IGNORECASE,
)


def _humanize_slug(slug: str) -> str:
    """Best-effort prettifier for a Greenhouse company slug.

    ``humanagency`` -> ``Humanagency``
    ``paradigminccareersopenpositions`` -> ``Paradigminccareersopenpositions``

    Not perfect (slugs are often run-together lowercase), but good
    enough for the ``applied_jobs.jsonl`` ``company_name`` fallback
    when the page itself doesn't surface a real name.
    """
    s = (slug or "").strip()
    if not s:
        return ""
    return s[:1].upper() + s[1:]


def _extract_role_and_company(page: Page) -> tuple[str, str, str]:
    """Return ``(role_title, company_slug, company_name)`` for the
    current Greenhouse application page.

    Strategy:
        * ``role_title``  -- prefer the page's first ``<h1>``; fall
          back to ``document.title`` (stripped of "- Apply" suffixes
          some boards add) and finally to "" if nothing usable.
        * ``company_slug`` -- the ``?for=...`` query param. Stable
          and lowercase; used as the per-company dedup key.
        * ``company_name`` -- best human-readable name we can guess
          from the JD's "ABOUT <COMPANY>" / "<Company> is..."
          patterns, falling back to the slug.
    """

    url = ""
    try:
        url = page.url or ""
    except PlaywrightError:
        url = ""
    slug = _extract_company_slug_from_url(url)

    # 1. Role: try <h1> first.
    role = ""
    try:
        h1 = page.locator("h1").first
        if h1.count() > 0:
            text = (h1.inner_text(timeout=1_000) or "").strip()
            if text and text.lower() not in ("back to jobs", "apply"):
                role = text
    except PlaywrightError:
        pass

    # Fall back to <title>.
    if not role:
        try:
            title = (page.title() or "").strip()
        except PlaywrightError:
            title = ""
        # Greenhouse sometimes uses the bare slug ("Back to jobs") as
        # the title -- those aren't useful.
        if title and title.lower() not in ("back to jobs", "apply", "greenhouse"):
            # Drop "- Apply" / "| Company" trailers some boards append.
            for sep in (" | ", " - ", " — "):
                if sep in title:
                    head, _, tail = title.partition(sep)
                    if any(
                        marker in tail.lower()
                        for marker in ("apply", "career", "job")
                    ):
                        title = head.strip()
                        break
            role = title

    # 2. Company name from JD body. Cheap to extract: just look at
    #    the first ~25 visible lines.
    company_name = ""
    try:
        body_text = page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
    except PlaywrightError:
        body_text = ""
    if isinstance(body_text, str) and body_text.strip():
        lines = [
            ln.strip() for ln in body_text.splitlines()[:60] if ln.strip()
        ]
        # Match "ABOUT FOO" headings (very common on Greenhouse).
        for ln in lines:
            up = ln.upper()
            if up.startswith("ABOUT "):
                cand = ln[6:].strip().rstrip(":").strip()
                # Reject generic "ABOUT US" / "ABOUT THE ROLE" /
                # "ABOUT THE TEAM"; only accept proper-noun-shaped
                # tails.
                low = cand.lower()
                if cand and low not in (
                    "us",
                    "the role",
                    "the team",
                    "this role",
                    "the position",
                    "the company",
                    "the job",
                ):
                    company_name = cand
                    break
        # Fallback: "<Company> is/was/builds/founded..." pattern.
        if not company_name:
            for ln in lines:
                m = _COMPANY_NAME_HINTS.match(ln)
                if m:
                    company_name = m.group(1).strip().rstrip(",.;:")
                    break
    if not company_name and slug:
        company_name = _humanize_slug(slug)

    return role, slug, company_name


def _extract_job_description(page: Page) -> str:
    """Pull the visible job description text from a Greenhouse
    application page. Returns ``""`` if nothing usable is found
    (e.g. an embed variant that hides the JD)."""

    try:
        body_text = page.evaluate("() => document.body.innerText || ''")
    except PlaywrightError:
        return ""
    if not isinstance(body_text, str):
        return ""

    text = body_text.strip()
    if not text:
        return ""

    # Slice off the application-form portion of the page.
    cut_at = -1
    for marker in _GREENHOUSE_JD_END_MARKERS:
        idx = text.find(marker)
        if idx > 0 and (cut_at == -1 or idx < cut_at):
            cut_at = idx
    if cut_at > 0:
        text = text[:cut_at]

    # Collapse runs of blank lines that result from the truncation,
    # and strip trailing whitespace on each line.
    cleaned_lines: list[str] = []
    last_blank = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            if not last_blank and cleaned_lines:
                cleaned_lines.append("")
            last_blank = True
            continue
        cleaned_lines.append(stripped)
        last_blank = False
    text = "\n".join(cleaned_lines).strip()

    if len(text) > _JOB_DESCRIPTION_MAX_CHARS:
        text = (
            text[:_JOB_DESCRIPTION_MAX_CHARS].rstrip()
            + "\n\n[...job description truncated for prompt size...]"
        )

    return text


# ---------------------------------------------------------------------------
# JD persistence (per-job .txt under logs/jds/)
# ---------------------------------------------------------------------------


_JD_DIR_NAME = Path("logs") / "jds"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(value: str, max_len: int = 60) -> str:
    """Sanitise a URL-derived string for use as a filename component."""
    cleaned = _SAFE_NAME_RE.sub("-", value).strip("-")
    return (cleaned or "job")[:max_len]


def _build_jd_filename(url: str) -> str:
    """Derive a stable filename from a Greenhouse job URL.

    Greenhouse job-board URLs look like::

        https://job-boards.greenhouse.io/embed/job_app
            ?for=cordial81&jr_id=...&token=8483382002&utm_source=jobright

    We extract ``for`` and ``jr_id`` (or ``token``) so the saved JD's
    filename is recognisable and stable across runs of the same job.
    """
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    company = _safe_filename_part(qs.get("for", ["job"])[0])
    ident = _safe_filename_part(
        qs.get("jr_id", qs.get("token", [""]))[0] or "",
        max_len=24,
    )
    timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"_{ident}" if ident else ""
    return f"{company}{suffix}_{timestamp}.txt"


def _save_job_description(
    url: str,
    title_hint: str,
    jd_text: str,
    *,
    project_root: Path | None = None,
) -> Path | None:
    """Persist ``jd_text`` to ``logs/jds/<company>_<id>_<ts>.txt``.

    The first line of the file is the source URL, the second the job
    title hint (when known), then a blank line, then the JD verbatim.
    Returns the path written, or ``None`` if writing failed (e.g. the
    JD was empty or the disk was unwritable).
    """
    text = (jd_text or "").strip()
    if not text:
        return None
    root = (project_root or Path.cwd()).resolve()
    dest_dir = (root / _JD_DIR_NAME).resolve()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create JD directory %s: %s", dest_dir, exc)
        return None
    path = dest_dir / _build_jd_filename(url)
    header = f"# URL: {url}\n"
    if title_hint:
        header += f"# Title: {title_hint}\n"
    header += "\n"
    try:
        path.write_text(header + text, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save JD to %s: %s", path, exc)
        return None
    return path


# ---------------------------------------------------------------------------
# Per-job resume selection (judge + matcher)
# ---------------------------------------------------------------------------


def _select_resume_for_job(
    profile: ApplicantProfile,
    jd_text: str,
    result: AutobidResult,
) -> ApplicantProfile:
    """If ``profile.resumes_dir`` is set and the JD passes the judge,
    swap the resume in ``profile`` to the best-matching PDF for this
    JD. Otherwise return the profile unchanged.

    The result's ``skip_reason`` / ``judge_details`` / ``resume_used``
    fields are populated as side effects.
    """
    if profile.resumes_dir is None:
        result.resume_used = profile.resume_path
        return profile
    if not jd_text.strip():
        # No JD captured -- the matcher would score every resume the
        # same way, so skip selection and use the configured default.
        logger.info(
            "No job description captured; per-job resume matching skipped, "
            "falling back to APPLICANT_RESUME_PATH."
        )
        result.resume_used = profile.resume_path
        return profile

    index = _get_resume_index(profile.resumes_dir)
    if not index.entries:
        result.resume_used = profile.resume_path
        return profile

    should_bid, reasons, details = judge_jd(jd_text, index)
    result.judge_details = details
    title = details.get("title") or "(no title)"
    if should_bid:
        logger.info(
            "JD judge: BID  -- %r (fit=%.2f, %d distinct tech phrases)",
            title,
            float(details.get("fit_score", 0.0)),
            int(details.get("distinct_tech_hits", 0)),
        )
    else:
        joined = "; ".join(reasons) if reasons else "no reason"
        logger.info(
            "JD judge: SKIP -- %r (%s)",
            title,
            joined,
        )
        result.skip_reason = joined
        result.resume_used = profile.resume_path
        return profile

    best, best_score, ranking = match_best_resume(jd_text, index)
    if best is None or best.path is None or best_score <= 0:
        logger.info(
            "Resume matcher returned no usable candidate; "
            "falling back to APPLICANT_RESUME_PATH."
        )
        result.resume_used = profile.resume_path
        return profile

    top_runners = ", ".join(
        f"{e.name} ({sc})" for sc, e in ranking[1:4] if sc > 0
    ) or "(none)"
    logger.info(
        "Resume matcher picked %s (score=%d). Runners-up: %s",
        best.name,
        best_score,
        top_runners,
    )
    result.resume_used = best.path

    # Re-extract text from the picked resume so the AI grounds answers
    # in the *new* CV rather than the default resume.pdf.
    picked_text = _extract_resume_text(best.path)
    return dataclasses.replace(
        profile,
        resume_path=best.path,
        resume_text=picked_text or profile.resume_text,
    )


# ---------------------------------------------------------------------------
# Survey -- gather every AI question on the page so they can all be sent
# to the model in a single batched API call. This is what keeps the
# OpenAI cost from scaling linearly with question count: rather than
# pay for the system prompt + resume context on every question, we pay
# for it once per job (sometimes twice if there are lazy-rendered
# follow-up questions).
#
# The survey is intentionally NON-DESTRUCTIVE: it never fills a field,
# never clicks an option. The only state-changing action it performs is
# briefly opening React-Select dropdowns to read their option labels,
# and it always closes them again with Escape.
# ---------------------------------------------------------------------------


def _survey_ai_questions(
    page: Page, profile: ApplicantProfile
) -> list[BatchItem]:
    """Walk the visible Greenhouse form and return one ``BatchItem`` for
    every field that would otherwise require its own AI API call."""

    items: list[BatchItem] = []
    seen_keys: set[tuple] = set()
    counter = [0]

    def emit(kind: str, ctx: QuestionContext) -> None:
        key = (kind, ctx.label, ctx.question, tuple(ctx.options))
        if key in seen_keys:
            return
        seen_keys.add(key)
        counter[0] += 1
        items.append(BatchItem(id=f"q{counter[0]}", kind=kind, ctx=ctx))

    _survey_text_questions(page, profile, emit)
    _survey_select_questions(page, profile, emit)
    _survey_radio_questions(page, profile, emit)
    _survey_checkbox_questions(page, profile, emit)

    return items


def _survey_text_questions(page, profile, emit) -> None:
    inputs = page.locator(_TEXT_INPUT_SELECTOR)
    try:
        count = inputs.count()
    except PlaywrightError:
        return
    for i in range(count):
        el = inputs.nth(i)
        try:
            if not el.is_visible():
                continue
            if (el.get_attribute("role") or "").lower() == "combobox":
                continue
            if (
                el.get_attribute("disabled") is not None
                or el.get_attribute("readonly") is not None
            ):
                continue
            if (el.input_value() or "").strip():
                continue
        except PlaywrightError:
            continue
        label = _get_label(page, el) or "(unlabelled)"
        # Don't ask the AI to fill optional Website / GitHub fields --
        # the user explicitly wants those left blank when the form
        # doesn't mark them required.
        if _is_optional_skippable_text(label, el):
            continue
        if _match_text_value(label, profile):
            continue
        question = _question_text_for_label(page, el, label)
        emit("answer", QuestionContext(label=label, question=question))


def _survey_select_questions(page, profile, emit) -> None:
    """Open each unknown React-Select transiently so we know the
    options to send to the AI. Pre-selected dropdowns and dropdowns
    matched by the profile are skipped (no AI needed)."""

    for combo_id in _collect_combobox_ids(page):
        if not combo_id:
            continue
        selector = _id_selector(combo_id)
        combo = page.locator(selector).first
        try:
            if combo.count() == 0 or not combo.is_visible():
                continue
            if _react_select_has_value(page, combo):
                continue
        except PlaywrightError:
            continue
        label = _get_label(page, combo) or "(unlabelled select)"
        is_multi = _is_multi_select_label(label)
        if _match_select_value(label, profile):
            # Profile will fill this; no AI needed.
            continue
        # Open + read options. Typeaheads (no static options) return ()
        # and are skipped here -- but typeaheads aren't AI-driven anyway.
        options = _open_and_read_options(page, combo)
        if not options:
            continue
        question = _question_text_for_label(page, combo, label)
        emit(
            "pick_multi" if is_multi else "pick_option",
            QuestionContext(label=label, question=question, options=options),
        )


def _survey_radio_questions(page, profile, emit) -> None:
    radios = page.locator('input[type="radio"]')
    try:
        count = radios.count()
    except PlaywrightError:
        return

    groups: dict[str, list[Locator]] = {}
    for i in range(count):
        r = radios.nth(i)
        try:
            name = (r.get_attribute("name") or "").strip()
        except PlaywrightError:
            continue
        if not _is_radio_group_visible(page, r):
            continue
        key = name or f"__anon_radio_{i}"
        groups.setdefault(key, []).append(r)

    for members in groups.values():
        if not members:
            continue
        try:
            if any(_safe_is_checked(m) for m in members):
                continue
        except PlaywrightError:
            continue
        question, label = _radio_group_label_and_question(page, members)
        options: list[str] = []
        for m in members:
            opt = _radio_or_checkbox_label_text(page, m)
            if opt and opt not in options:
                options.append(opt)
        if not options:
            continue
        if _match_option_label(label, options, profile):
            continue
        emit(
            "pick_option",
            QuestionContext(
                label=label,
                question=question,
                options=tuple(options),
            ),
        )


def _survey_checkbox_questions(page, profile, emit) -> None:
    boxes = page.locator('input[type="checkbox"]')
    try:
        count = boxes.count()
    except PlaywrightError:
        return

    groups: dict[str, list[Locator]] = {}
    for i in range(count):
        b = boxes.nth(i)
        try:
            name = (b.get_attribute("name") or "").strip()
        except PlaywrightError:
            continue
        if not _is_radio_group_visible(page, b):
            continue
        key = name or f"__anon_checkbox_{i}"
        groups.setdefault(key, []).append(b)

    for members in groups.values():
        if not members:
            continue
        if any(_safe_is_checked(m) for m in members):
            continue

        if len(members) == 1:
            box = members[0]
            # Required acknowledgements are auto-checked, no AI needed.
            try:
                if (
                    box.get_attribute("required") is not None
                    or (box.get_attribute("aria-required") or "").lower()
                    == "true"
                ):
                    continue
            except PlaywrightError:
                continue
            label = (
                _radio_or_checkbox_label_text(page, box)
                or _get_label(page, box)
                or "(unlabelled checkbox)"
            )
            question = _question_text_for_label(page, box, label)
            emit(
                "pick_option",
                QuestionContext(
                    label=label,
                    question=(
                        "Should this checkbox be checked? Reply with "
                        'exactly "Yes" or "No". Question:\n' + question
                    ),
                    options=("Yes", "No"),
                ),
            )
            continue

        # Multi-checkbox group.
        question, label = _radio_group_label_and_question(page, members)
        options: list[str] = []
        for m in members:
            opt = _radio_or_checkbox_label_text(page, m)
            if opt and opt not in options:
                options.append(opt)
        if not options:
            continue
        if _match_option_label(label, options, profile):
            continue
        emit(
            "pick_multi",
            QuestionContext(
                label=label,
                question=question,
                options=tuple(options),
            ),
        )


# ---------------------------------------------------------------------------
# Greenhouse filler
# ---------------------------------------------------------------------------


# Lower-case label substring -> profile attribute name. The first matching
# entry (in iteration order) wins, so put more specific keys before generic
# ones.
# CSS selector for every kind of free-text input we want the autobid
# to fill. Includes ``type=number`` so native spinbutton year fields
# (rendered by some Greenhouse education sections, e.g. cordial81 /
# agency / pluspower) are picked up by the same path that fills text
# inputs and textareas.
_TEXT_INPUT_SELECTOR = (
    'input:visible[type="text"], '
    'input:visible[type="email"], '
    'input:visible[type="tel"], '
    'input:visible[type="url"], '
    'input:visible[type="number"], '
    'input:visible[type="search"], '
    "textarea:visible"
)


_TEXT_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("first name", "first_name"),
    ("preferred first name", "preferred_name"),
    ("preferred name", "preferred_name"),
    ("last name", "last_name"),
    ("full name", "full_name"),
    ("email", "email"),
    ("phone", "phone"),
    ("linkedin", "linkedin"),
    ("github", "github"),
    ("website", "website"),
    ("portfolio", "website"),
    # IMPORTANT: combined "City, State" / "City and State" fields must
    # match BEFORE plain "city" so they fill as e.g. "Waco, TX" rather
    # than just "Waco". Order matters here -- _match_text_value picks
    # the first substring hit.
    ("city, state", "city_state"),
    ("city and state", "city_state"),
    ("city & state", "city_state"),
    ("city/state", "city_state"),
    ("city or state", "city_state"),
    ("location (city, state)", "city_state"),
    ("city", "city"),
    ("state", "state"),
    ("province", "state"),
    ("postal code", "postal_code"),
    ("zip code", "postal_code"),
    ("zip", "postal_code"),
    # Numeric education year fields are sometimes rendered as native
    # <input type="number"> spinbuttons (e.g. cordial81 / agency /
    # pluspower) instead of React-Select dropdowns. The text-field
    # filler now handles type=number too, so map them here.
    ("start date year", "education_start_year"),
    ("start year", "education_start_year"),
    ("from year", "education_start_year"),
    ("end date year", "education_end_year"),
    ("end year", "education_end_year"),
    ("graduation year", "education_end_year"),
    # Free-text answers we don't want the AI to invent.
    ("how did you hear", "referral_source"),
    ("hear about", "referral_source"),
    ("referred you", "referral_source"),
    ("referral source", "referral_source"),
)

_SELECT_FIELD_MAP: tuple[tuple[str, str], ...] = (
    # IMPORTANT: more specific keys must come *before* generic ones because
    # _match_select_value uses first-substring-match. e.g. "gender identity"
    # has to match before plain "gender", and "racial/ethnic" before "race".
    ("gender identity", "gender_identity"),
    ("racial/ethnic", "racial_ethnic_background"),
    ("racial / ethnic", "racial_ethnic_background"),
    ("ethnic background", "racial_ethnic_background"),
    ("sexual orientation", "sexual_orientation"),
    ("transgender", "transgender"),

    # The "U.S. Standard Demographic" survey single-selects (Paradigm
    # uses these on top of the EEO ones below). Match BEFORE generic
    # "disability" / "veteran".
    ("disability or chronic", "disability_chronic"),
    ("chronic condition", "disability_chronic"),
    ("active member", "veteran_active"),
    ("armed forces", "veteran_active"),

    # Common open-ended dropdowns we want pulled from .env when set.
    ("how did you hear", "referral_source"),
    ("hear about", "referral_source"),
    ("referred you", "referral_source"),
    ("security clearance", "security_clearance"),
    ("clearance type", "security_clearance"),

    # Some "Where will you be working from?" / "City, state" prompts
    # are rendered as React-Select typeaheads rather than text inputs.
    ("where will you be working", "city_state"),
    ("city, state", "city_state"),
    ("city and state", "city_state"),
    ("city & state", "city_state"),

    ("country", "country"),
    ("gender", "gender"),
    # IMPORTANT: only the explicit Yes/No "Are you Hispanic/Latino?" label
    # should pull from APPLICANT_HISPANIC_ETHNICITY ("Yes"/"No"). A bare
    # "Ethnicity" label on a Greenhouse form almost always means a
    # *combined* race/ethnicity dropdown whose options look like
    # "White (not Hispanic or Latino)". If we mapped that to
    # hispanic_ethnicity = "No" the bot would type "No", every option
    # would match the "(not Hispanic or Latino)" suffix, and React-Select
    # would pick the first one alphabetically -- which is
    # "American Indian/Alaskan Native...". Map it to RACE instead so we
    # type the actual ethnicity (e.g. "White") and the typeahead
    # narrows to the correct row.
    ("hispanic", "hispanic_ethnicity"),
    ("race", "race"),
    ("ethnicity", "race"),
    ("veteran", "veteran_status"),
    ("disability", "disability_status"),
    # Education / location typeaheads. Order matters: more specific
    # substrings come first so e.g. "field of study" matches before plain
    # "field". "start" / "end" are deliberately broad enough to catch
    # "Start Date Year", "Start Year", and "From".
    ("location (city)", "location_city"),
    ("location city", "location_city"),
    ("location", "location_city"),
    ("school", "school"),
    ("university", "school"),
    ("college", "school"),
    ("institution", "school"),
    ("degree", "degree"),
    ("field of study", "discipline"),
    ("major", "discipline"),
    ("discipline", "discipline"),
    # Education month fields. Place these BEFORE the year mappings so
    # e.g. "Start date month" matches the month attr first; the year-
    # only guard below also protects against accidental crossover.
    ("start date month", "education_start_month"),
    ("start month", "education_start_month"),
    ("from month", "education_start_month"),
    ("end date month", "education_end_month"),
    ("end month", "education_end_month"),
    ("to month", "education_end_month"),
    ("graduation month", "education_end_month"),
    # Year fields. We only have year values in the profile, so for forms
    # with a separate "Start Month" / "End Month" select we let the AI
    # pick (see year-only guard in _match_select_value below).
    ("start date year", "education_start_year"),
    ("start year", "education_start_year"),
    ("from year", "education_start_year"),
    ("start date", "education_start_year"),
    ("end date year", "education_end_year"),
    ("end year", "education_end_year"),
    ("to year", "education_end_year"),
    ("graduation year", "education_end_year"),
    ("end date", "education_end_year"),
)


# Profile attributes that store *year-only* values; never let them
# match a label that's actually asking for a month or a day, even if a
# broader needle (e.g. "start date") would otherwise match.
_YEAR_ONLY_ATTRS = frozenset({"education_start_year", "education_end_year"})

# Profile attributes that store *month-only* values. These should never
# match a label that's actually asking for a year or a day (the user
# may set APPLICANT_EDUCATION_START_MONTH=September while leaving the
# year separate).
_MONTH_ONLY_ATTRS = frozenset({"education_start_month", "education_end_month"})

# Text-input labels that should be left blank when the form marks the
# field as optional (no `aria-required="true"`). The user explicitly
# does not want Website / GitHub / portfolio links auto-filled on
# forms that don't require them.
_OPTIONAL_TEXT_LABEL_NEEDLES = ("website", "github", "portfolio")


def _fill_greenhouse(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    *,
    submit: bool,
    action_timeout_ms: int,
) -> AutobidResult:
    result = AutobidResult(provider="greenhouse", url=page.url)

    # Capture role + company metadata as early as possible so callers
    # have it for ``applied_jobs.jsonl`` / ``skipped_jobs.jsonl`` even
    # if the form load times out below.
    role, slug, company = _extract_role_and_company(page)
    result.role_title = role
    result.company_slug = slug
    result.company_name = company
    if role or company or slug:
        logger.info(
            "Job metadata: role=%r, company=%r (slug=%r)",
            role,
            company,
            slug,
        )

    try:
        page.wait_for_selector("form", timeout=action_timeout_ms)
    except PlaywrightTimeoutError:
        result.error = "Greenhouse form did not load in time."
        return result

    # 0. Capture the job description -- needed by both the AI answer
    #    generator AND the per-job resume judge/matcher. We extract the
    #    JD even when ``ai`` is None, because we still want to save the
    #    JD to disk and run the BID/SKIP judge.
    try:
        jd = _extract_job_description(page)
    except Exception as exc:
        logger.debug("Job description extraction failed: %s", exc)
        jd = ""
    if jd:
        logger.info(
            "Captured %d chars of job description from page.", len(jd)
        )
    else:
        logger.info(
            "No job description found on page; AI will fall back to "
            "candidate profile + resume only."
        )

    # 0a. Save the JD to disk for record-keeping (logs/jds/<company>_<ts>.txt).
    title_hint = ""
    if jd:
        for line in jd.splitlines():
            if line.strip():
                title_hint = line.strip()
                break
    result.jd_path = _save_job_description(page.url, title_hint, jd)
    if result.jd_path is not None:
        logger.info("Saved job description to %s", result.jd_path)

    # 0b. Judge whether this job is worth bidding on, and -- if BID --
    #     pick the best-matching resume from APPLICANT_RESUMES_DIR.
    profile = _select_resume_for_job(profile, jd, result)
    if result.skip_reason is not None:
        logger.info(
            "Skipping autobid fill for this job (judge: %s).",
            result.skip_reason,
        )
        return result

    # 0c. Now that we know which resume we're using, set the AI context.
    #
    # IMPORTANT: ``ai.profile`` was bound ONCE at process startup from
    # ``APPLICANT_RESUME_PATH`` (i.e. resume.pdf). The per-job matcher
    # may have just swapped ``profile.resume_text`` to the matched
    # PDF's text, but unless we re-point ``ai.profile`` at that new
    # profile, every prompt the AI renders for this job would still
    # read from the static resume.pdf -- which is exactly the bug
    # we're fixing here. The ``ai.job_description`` setter clears the
    # answer cache when the JD changes, so we don't have to worry
    # about stale cached answers from the old resume.
    if ai is not None:
        ai.profile = profile
        ai.job_description = jd
        if jd:
            logger.info(
                "AI answers will be tailored to this role and "
                "grounded in %s.",
                profile.resume_path.name if profile.resume_path else "resume",
            )

    # 1. Resume upload first -- some forms reveal additional fields after a
    #    resume is parsed.
    if profile.resume_path and profile.resume_path.exists():
        try:
            _upload_resume(page, str(profile.resume_path))
            result.filled.append("Resume/CV")
            logger.info("Uploaded resume: %s", profile.resume_path)
        except Exception as exc:
            logger.warning("Resume upload failed: %s", exc)
            result.failed.append(f"Resume/CV ({exc})")
    elif profile.resume_path:
        logger.warning(
            "APPLICANT_RESUME_PATH points to %s but the file does not exist.",
            profile.resume_path,
        )
        result.skipped.append("Resume/CV (file not found)")
    else:
        result.skipped.append("Resume/CV (no path configured)")

    # 2. SURVEY -- one DOM walk to identify every field that will need
    #    AI input (text answer, dropdown pick, radio pick, checkbox
    #    decision, multi-checkbox pick). Then send them all in a SINGLE
    #    AI API call so we don't pay for the resume context once per
    #    question. Results land in AIAnswerer's cache; the regular fill
    #    walk that follows looks them up locally.
    if ai is not None:
        try:
            survey_items = _survey_ai_questions(page, profile)
        except Exception as exc:
            # Surveying involves opening dropdowns to read options; if
            # anything goes wrong we just fall back to the per-question
            # path (which still works, just costs more).
            logger.warning(
                "Survey of AI questions failed (%s); falling back to "
                "per-question API calls.",
                exc,
            )
            survey_items = []
        if survey_items:
            logger.info(
                "Surveyed %d AI question(s); sending in one batched API call.",
                len(survey_items),
            )
            ai.batch_prepare(survey_items)
        else:
            logger.debug("Survey found no AI questions to batch.")

    # 3. Text inputs and textareas.
    _fill_text_fields(page, profile, ai, result)

    # 4. React-Select dropdowns.
    _fill_select_fields(page, profile, ai, result)

    # 5. Radio button groups (single-select questions rendered as radios
    #    rather than dropdowns -- common for sponsorship / yes-no).
    _fill_radio_groups(page, profile, ai, result)

    # 6. Checkboxes (single acknowledgements + multi-select groups).
    _fill_checkbox_fields(page, profile, ai, result)

    # 7. Lazy-rendered AI questions (rare but possible -- e.g. a
    #    follow-up question that only renders after a previous answer).
    #    A second survey + batch + fill catches these in at most ONE
    #    extra API call rather than N.
    if ai is not None:
        try:
            late_items = _survey_ai_questions(page, profile)
        except Exception:
            late_items = []
        # ``batch_prepare`` skips items already in the cache, so this
        # only sends genuinely new questions.
        if late_items:
            new_count = ai.batch_prepare(late_items)
            if new_count > 0:
                logger.info(
                    "Lazy-rendered AI questions appeared after first fill; "
                    "running fill walk again to populate them.",
                )
                _fill_text_fields(page, profile, ai, result)
                _fill_select_fields(page, profile, ai, result)
                _fill_radio_groups(page, profile, ai, result)
                _fill_checkbox_fields(page, profile, ai, result)

    # 8. Verify nothing got cleared by later interactions and refill if so.
    #    React-controlled forms (especially with IntlTelInput on phone, and
    #    the way React-Select blurs earlier inputs when opening) sometimes
    #    drop values we set. We do up to 2 passes -- if a field re-clears
    #    on the second pass we give up to avoid infinite retry loops.
    _verify_and_refill(page, result, max_passes=2)

    if ai is not None:
        # In the old per-question pipeline, every AI-answered field
        # cost one API call. Now most fields are served from a shared
        # batch cache. ``distinct_served`` counts unique cache keys
        # consumed (so it doesn't double-count if a verify pass happens
        # to look up the same question twice).
        distinct = len(ai.distinct_served)
        logger.info(
            "AI usage for this job: %d API call(s); %d field(s) served "
            "from the batch cache (would have been ~%d API calls in the "
            "per-question pipeline).",
            ai.api_calls,
            distinct,
            distinct + max(0, ai.api_calls - 1),
        )

    # 9. Submit (or dry-run).
    if submit:
        _submit_application(page, result, action_timeout_ms=action_timeout_ms)
    else:
        logger.info(
            "DRY RUN -- form filled but not submitted. Pass --submit to actually submit."
        )

    return result


# ---------------------------------------------------------------------------
# Text inputs / textareas
# ---------------------------------------------------------------------------


def _fill_text_fields(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    inputs = page.locator(_TEXT_INPUT_SELECTOR)
    try:
        count = inputs.count()
    except PlaywrightError:
        count = 0

    for i in range(count):
        el = inputs.nth(i)
        try:
            if not el.is_visible():
                continue
            # Skip hidden / disabled / readonly fields and the React-Select
            # internal input (which shares ``role=combobox``).
            role = (el.get_attribute("role") or "").lower()
            if role == "combobox":
                continue
            if (el.get_attribute("disabled") is not None
                    or el.get_attribute("readonly") is not None):
                continue
        except PlaywrightError:
            continue

        label = _get_label(page, el) or "(unlabelled)"
        existing = ""
        try:
            existing = (el.input_value() or "").strip()
        except PlaywrightError:
            pass
        if existing:
            logger.debug("Skipping pre-filled field %r (value=%r)", label, existing)
            result.skipped.append(f"{label} (already filled)")
            continue

        # User preference: leave optional Website / GitHub / portfolio
        # links blank. Filling them otherwise would either leak a profile
        # value the user wants kept private OR -- worse -- trigger an
        # AI-generated URL on forms where the field has no profile match.
        if _is_optional_skippable_text(label, el):
            logger.info(
                "Skipping optional %r (not required, leaving blank).",
                label,
            )
            result.skipped.append(f"{label} (optional, left blank)")
            continue

        # Stable selector for re-locating during verification pass.
        try:
            el_id = (el.get_attribute("id") or "").strip()
        except PlaywrightError:
            el_id = ""
        selector = _id_selector(el_id)

        value = _match_text_value(label, profile)
        if value:
            if _safe_fill(el, value):
                logger.info("Filled %r = %r", label, value)
                result.filled.append(label)
                if selector:
                    result._tracker.append(
                        _FilledField(label=label, selector=selector, value=value, kind="text")
                    )
            else:
                result.failed.append(f"{label} (fill error)")
            continue

        # Unknown text field -- try AI if available and the field is required
        # OR we have an AI client at all.
        if ai is None:
            logger.info("Leaving unknown text field %r blank (AI disabled).", label)
            result.skipped.append(f"{label} (AI disabled)")
            continue

        question = _question_text_for_label(page, el, label)
        ans = ai.answer(QuestionContext(label=label, question=question))
        if ans and _safe_fill(el, ans):
            logger.info("AI-filled %r", label)
            result.filled.append(f"{label} (AI)")
            if selector:
                result._tracker.append(
                    _FilledField(label=label, selector=selector, value=ans, kind="text")
                )
        else:
            result.skipped.append(f"{label} (no AI answer)")


def _match_text_value(label: str, profile: ApplicantProfile) -> str:
    norm = label.lower().strip()
    for needle, attr in _TEXT_FIELD_MAP:
        if needle in norm:
            return getattr(profile, attr, "") or ""
    return ""


# ---------------------------------------------------------------------------
# React-Select dropdowns
# ---------------------------------------------------------------------------


def _fill_select_fields(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    # We iterate by *id* (re-enumerated each pass) rather than by index, for
    # two reasons:
    #   1. `combos.nth(i)` against a `:visible`-filtered locator is fragile:
    #      opening a dropdown / selecting an option can briefly shift
    #      visibility or order, causing comboboxes far down the page
    #      (notably Disability Status) to be silently skipped.
    #   2. Greenhouse's self-id section renders some comboboxes lazily --
    #      e.g. "Please identify your race" only appears after the
    #      Hispanic/Latino question has been answered. A single up-front
    #      snapshot would miss those.
    processed_ids: set[str] = set()
    max_passes = 15  # safety cap; in practice 2-3 is plenty.

    for pass_num in range(1, max_passes + 1):
        combo_ids = _collect_combobox_ids(page)
        new_ids = [cid for cid in combo_ids if cid and cid not in processed_ids]
        if not new_ids:
            logger.debug(
                "Combobox enumeration converged after %d pass(es); processed %d total.",
                pass_num - 1,
                len(processed_ids),
            )
            break
        logger.debug(
            "Combobox pass %d: %d new id(s) %s (already processed: %d)",
            pass_num,
            len(new_ids),
            new_ids,
            len(processed_ids),
        )

        for combo_id in new_ids:
            processed_ids.add(combo_id)
            _process_react_select(
                page, combo_id, profile, ai, result
            )
    else:
        logger.warning(
            "Combobox enumeration did not converge after %d passes; "
            "stopping to avoid an infinite loop.",
            max_passes,
        )


def _process_react_select(
    page: Page,
    combo_id: str,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    selector = _id_selector(combo_id)
    combo = page.locator(selector).first
    try:
        if combo.count() == 0:
            return
    except PlaywrightError:
        return

    # Bring it into view so visibility / interaction work even for
    # comboboxes far below the fold (Disability Status is way at the
    # bottom of long Greenhouse forms).
    try:
        combo.scroll_into_view_if_needed(timeout=2_000)
    except PlaywrightError:
        pass

    try:
        if not combo.is_visible():
            logger.debug("Combobox #%s is not visible; skipping.", combo_id)
            return
    except PlaywrightError:
        return

    label = _get_label(page, combo) or "(unlabelled select)"
    is_multi = _is_multi_select_label(label)

    # Already filled via free-form fallback in a prior pass? Don't try
    # again -- the typed text is already in the input, and re-running
    # the typeahead would just reproduce the same "no suggestions"
    # condition (and on flaky pages it can fail outright the second
    # time, turning a soft warning into a hard failure).
    if combo_id in result._freeform_combo_ids:
        logger.debug(
            "Skipping %r -- already committed via free-form fallback.",
            label,
        )
        return

    # Already has a value?
    if _react_select_has_value(page, combo):
        logger.debug("Skipping pre-selected dropdown %r.", label)
        result.skipped.append(f"{label} (already selected)")
        return

    target = _match_select_value(label, profile)

    # ------------------------------------------------------------------
    # Multi-select ("(mark all that apply)") path.
    # ------------------------------------------------------------------
    if is_multi:
        targets = _split_multi_targets(target)
        # If profile didn't supply anything, fall back to AI's pick_multi.
        if not targets and ai is not None:
            options = _open_and_read_options(page, combo)
            if options:
                question = _question_text_for_label(page, combo, label)
                ai_choices = ai.pick_multi(
                    QuestionContext(
                        label=label, question=question, options=options
                    )
                )
                targets = list(ai_choices) if ai_choices else []
        if not targets:
            logger.info(
                "Leaving multi-select %r empty (no profile value, "
                "no AI available).",
                label,
            )
            result.skipped.append(f"{label} (no targets)")
            _press_escape(page)
            return
        picked = _select_react_multi_options(page, combo, targets)
        if picked > 0:
            logger.info(
                "Multi-selected %d/%d option(s) in %r: %s",
                picked,
                len(targets),
                label,
                targets,
            )
            result.filled.append(
                f"{label} ({picked} option{'s' if picked != 1 else ''})"
            )
            # Don't add multi-select to the verify tracker: each option
            # is its own chip and the existing single-value-string-based
            # verify pass can't cleanly re-apply a list.
        else:
            logger.warning(
                "Could not pick any of %s in multi-select %r.",
                targets,
                label,
            )
            result.failed.append(f"{label} (no multi-select option matched)")
        return

    # ------------------------------------------------------------------
    # Single-select path: try profile value first, fall back to AI.
    # ------------------------------------------------------------------
    if target:
        if _select_react_option(page, combo, target):
            # Did we actually click a suggestion, or did we fall back to
            # leaving free-form text? React-Select only renders
            # ``.select__single-value`` for true selections.
            if _react_select_has_value(page, combo):
                logger.info("Selected %r = %r", label, target)
                result.filled.append(label)
                result._tracker.append(
                    _FilledField(label=label, selector=selector, value=target, kind="select")
                )
                return
            else:
                logger.warning(
                    "No suggestion matched %r for %r; left typed value as "
                    "free-form text. Form may still accept it.",
                    target,
                    label,
                )
                # Don't add to _tracker -- verify-and-refill would
                # otherwise loop on this since there's no committed value.
                result.filled.append(f"{label} (free-form: {target!r})")
                result._freeform_combo_ids.add(combo_id)
                return
        # _select_react_option returned False: profile value isn't an
        # option on THIS form (e.g. profile.gender="Male" but the form
        # only offers "Man / Woman / Non-binary"). Fall through to the
        # AI path so we still try to give a sensible answer rather
        # than hard-failing the field.
        if ai is None:
            logger.warning(
                "Could not select %r in %r; option may not exist.",
                target,
                label,
            )
            result.failed.append(f"{label} (option {target!r} not found)")
            return
        logger.info(
            "Profile value %r not in options for %r; falling back to AI.",
            target,
            label,
        )
        # Make sure the dropdown is closed before the AI path re-opens it.
        _press_escape(page)

    # Unknown dropdown OR profile-match failed -- ask AI, but only if available.
    if ai is None:
        logger.info("Leaving unknown dropdown %r empty (AI disabled).", label)
        result.skipped.append(f"{label} (AI disabled)")
        return

    options = _open_and_read_options(page, combo)
    if not options:
        result.skipped.append(f"{label} (no options found)")
        return

    question = _question_text_for_label(page, combo, label)
    ai_choice = ai.pick_option(
        QuestionContext(label=label, question=question, options=options)
    )
    if ai_choice and _select_react_option(page, combo, ai_choice):
        logger.info("AI-selected %r in %r", ai_choice, label)
        result.filled.append(f"{label} (AI)")
        result._tracker.append(
            _FilledField(label=label, selector=selector, value=ai_choice, kind="select")
        )
    else:
        # Make sure we close the dropdown so subsequent fields work.
        _press_escape(page)
        result.skipped.append(f"{label} (no AI choice)")


def _verify_and_refill(
    page: Page,
    result: AutobidResult,
    *,
    max_passes: int = 2,
) -> None:
    """Re-check every field we previously filled and re-apply the value if
    React (or IntlTelInput, or React-Select side effects) has cleared it.

    Greenhouse forms occasionally drop a text input's value when a later
    React-Select dropdown opens and blurs the focused element. We do a
    bounded number of passes; if a field re-clears every time, we log it
    as failed instead of looping forever.
    """

    if not result._tracker:
        return

    for pass_num in range(1, max_passes + 1):
        any_refilled = False
        cleared: list[str] = []

        for ff in result._tracker:
            if not ff.selector:
                continue
            try:
                el = page.locator(ff.selector).first
                if el.count() == 0:
                    continue
                if not el.is_visible():
                    continue
            except PlaywrightError:
                continue

            if ff.kind == "text":
                try:
                    current = (el.input_value(timeout=1_000) or "").strip()
                except PlaywrightError:
                    continue
                if current:
                    continue
                cleared.append(ff.label)
                logger.warning(
                    "[verify pass %d] Field %r is empty; re-filling with %r.",
                    pass_num,
                    ff.label,
                    ff.value,
                )
                if _safe_fill(el, ff.value):
                    # Commit by tabbing out -- many React-controlled inputs
                    # only flush state to the form on blur.
                    try:
                        el.press("Tab", timeout=1_000)
                    except PlaywrightError:
                        pass
                    any_refilled = True

            elif ff.kind == "select":
                if _react_select_has_value(page, el):
                    continue
                cleared.append(ff.label)
                logger.warning(
                    "[verify pass %d] Dropdown %r is empty; re-selecting %r.",
                    pass_num,
                    ff.label,
                    ff.value,
                )
                if _select_react_option(page, el, ff.value):
                    any_refilled = True

        if not cleared:
            logger.info(
                "[verify pass %d] All %d filled fields still hold their values.",
                pass_num,
                len(result._tracker),
            )
            return

        if any_refilled:
            result.refilled.extend(cleared)

        # Give React a beat to settle before the next verification pass.
        page.wait_for_timeout(400)

    # Final sweep: anything still empty after max_passes is a true failure.
    for ff in result._tracker:
        if not ff.selector:
            continue
        try:
            el = page.locator(ff.selector).first
            if el.count() == 0 or not el.is_visible():
                continue
        except PlaywrightError:
            continue
        if ff.kind == "text":
            try:
                current = (el.input_value(timeout=1_000) or "").strip()
            except PlaywrightError:
                continue
            if not current:
                logger.error(
                    "Field %r remained empty after %d refill attempts.",
                    ff.label,
                    max_passes,
                )
                result.failed.append(f"{ff.label} (kept clearing)")
        elif ff.kind == "select":
            if not _react_select_has_value(page, el):
                logger.error(
                    "Dropdown %r remained empty after %d refill attempts.",
                    ff.label,
                    max_passes,
                )
                result.failed.append(f"{ff.label} (kept clearing)")


def _match_select_value(label: str, profile: ApplicantProfile) -> str:
    norm = label.lower().strip()
    # Some Greenhouse education sections split the date into separate
    # "Start date month" / "Start date year" / "End date month" / etc.
    # selects. The bare "start date" / "end date" keys would greedily
    # match the *month* selects and incorrectly stuff a year into them,
    # so we skip year-only attrs whenever the label is actually a
    # month/day field, and similarly skip month-only attrs when the
    # label is asking for a year. The AI fallback then picks an
    # appropriate value.
    label_is_month_or_day = ("month" in norm) or ("day" in norm)
    label_is_year = ("year" in norm) and not label_is_month_or_day
    for needle, attr in _SELECT_FIELD_MAP:
        if needle in norm:
            if label_is_month_or_day and attr in _YEAR_ONLY_ATTRS:
                continue
            if label_is_year and attr in _MONTH_ONLY_ATTRS:
                continue
            return getattr(profile, attr, "") or ""
    return ""


def _is_multi_select_label(label: str) -> bool:
    """Return True if a label looks like a "(mark all that apply)"
    multi-select question. Greenhouse's multi-select React-Select does
    NOT expose ``aria-multiselectable=true`` on the input -- the only
    reliable hint is the question text itself."""

    norm = label.lower()
    return (
        "all that apply" in norm
        or "mark all" in norm
        or "select all" in norm
        or "(check all" in norm
    )


def _split_multi_targets(value: str) -> list[str]:
    """Split a comma-separated multi-select profile value into a list of
    individual option labels, dropping blanks and whitespace."""

    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [p for p in parts if p]


def _is_aria_required(el: Locator) -> bool:
    """Whether a form input is marked required via ``aria-required``
    or the standard ``required`` attribute."""

    try:
        if (el.get_attribute("aria-required") or "").lower() == "true":
            return True
        if el.get_attribute("required") is not None:
            return True
    except PlaywrightError:
        pass
    return False


def _is_optional_skippable_text(label: str, el: Locator) -> bool:
    """Whether to leave a text input blank because the user does not
    want it auto-filled when optional (currently Website / GitHub /
    portfolio fields)."""

    norm = label.lower()
    if not any(needle in norm for needle in _OPTIONAL_TEXT_LABEL_NEEDLES):
        return False
    return not _is_aria_required(el)


def _collect_combobox_ids(page: Page) -> list[str]:
    """Return the ``id`` of every React-Select combobox on the page.

    We use a JS one-shot rather than a Playwright Locator iteration because
    the Locator approach is sensitive to in-flight DOM mutations and can
    silently miss late-rendered comboboxes (e.g. Greenhouse's Disability
    Status select that lives at the very bottom of a tall form).
    """

    try:
        ids = page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll(
                'input[role="combobox"][aria-haspopup="true"]'
              ));
              return nodes.map(n => n.id || '');
            }
            """
        )
    except PlaywrightError as exc:
        logger.warning("Could not enumerate comboboxes via JS: %s", exc)
        return []
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids]


def _react_select_has_value(page: Page, combo: Locator) -> bool:
    """A React-Select control with a chosen value renders a
    ``.select__single-value`` (or multi-value) inside its container."""

    try:
        container = combo.locator(
            'xpath=ancestor::*[contains(@class,"select__control")][1]'
        )
        if container.count() == 0:
            return False
        sv = container.locator(".select__single-value, .select__multi-value")
        return sv.count() > 0
    except PlaywrightError:
        return False


def _open_react_select(page: Page, combo: Locator) -> str:
    """Click the combobox so it's interactive.

    Returns one of:

    * ``"open"``   -- the combobox was clicked AND a listbox is visible.
    * ``"focused"`` -- the combobox was clicked but no listbox appeared
      (typical of typeahead React-Selects that don't render options until
      the user types).
    * ``"fail"``   -- could not click the combobox at all.
    """

    try:
        combo.scroll_into_view_if_needed(timeout=2_000)
    except PlaywrightError:
        pass

    try:
        combo.click(timeout=4_000)
    except PlaywrightError as exc:
        # Some Greenhouse forms briefly cover their comboboxes with a
        # sticky overlay (banner, file-chooser host, etc.) which makes
        # the actionable click time out. Fall back to a forced click
        # and then a plain focus(): React-Select also opens / accepts
        # typeahead from a focused input. If even focus fails we give
        # up on this combobox.
        logger.debug("Standard combobox click failed (%s); trying force.", exc)
        try:
            combo.click(timeout=2_000, force=True)
        except PlaywrightError as exc2:
            logger.debug("Force click also failed (%s); trying focus.", exc2)
            try:
                combo.focus(timeout=2_000)
            except PlaywrightError as exc3:
                logger.warning(
                    "Could not click or focus combobox: %s", exc3
                )
                return "fail"

    combo_id = combo.get_attribute("id") or ""
    listbox_selector = (
        f'[id^="react-select-{combo_id}-listbox"], '
        f'[id="{combo_id}-listbox"]'
    )

    def _listbox_is_visible(timeout_ms: int) -> bool:
        try:
            page.locator(listbox_selector).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        except PlaywrightTimeoutError:
            try:
                page.locator('[role="listbox"]:visible').first.wait_for(
                    state="visible", timeout=min(timeout_ms, 1_000)
                )
                return True
            except PlaywrightTimeoutError:
                return False

    if _listbox_is_visible(1_500):
        return "open"

    # Some Greenhouse forms (e.g. Paradigm's "U.S. Standard Demographic"
    # selects) configure their React-Select with openMenuOnClick=false:
    # plain mouse-click focuses the input but doesn't expand the
    # listbox. The keyboard ArrowDown shortcut DOES open it, so try
    # that before falling through to the typeahead-only path.
    try:
        combo.focus(timeout=1_500)
        page.keyboard.press("ArrowDown")
    except PlaywrightError:
        pass

    if _listbox_is_visible(1_000):
        return "open"

    logger.debug(
        "Listbox did not appear after clicking combobox %r; "
        "treating as focused-only (likely a typeahead).",
        combo_id,
    )
    return "focused"


def _open_and_read_options(page: Page, combo: Locator) -> tuple[str, ...]:
    state = _open_react_select(page, combo)
    if state == "fail":
        return ()
    if state == "focused":
        # Typeahead with no static options. We can't enumerate options
        # without typing first, and we have no query yet, so close.
        _press_escape(page)
        return ()
    try:
        # Prefer the listbox tied to this combobox; fall back to any visible.
        combo_id = combo.get_attribute("id") or ""
        listbox = page.locator(
            f'[id^="react-select-{combo_id}-listbox"]'
        )
        if listbox.count() == 0:
            listbox = page.locator('[role="listbox"]:visible').first
        else:
            listbox = listbox.first
        options = listbox.locator('[role="option"]')
        count = options.count()
        labels: list[str] = []
        for i in range(count):
            try:
                txt = options.nth(i).inner_text(timeout=500).strip()
            except PlaywrightError:
                continue
            if txt:
                labels.append(txt)
        # Close the dropdown so the next field interacts cleanly.
        _press_escape(page)
        return iter_options(labels)
    except PlaywrightError:
        _press_escape(page)
        return ()


def _select_react_option(page: Page, combo: Locator, target: str) -> bool:
    """Open a React-Select dropdown and click the option that best matches
    ``target``.

    Greenhouse uses two flavours of React-Select on its forms:

    * **Static** -- all options are present in the DOM as soon as the
      dropdown opens. We pick the first exact / substring match.
    * **Typeahead** -- the listbox is empty (or shows "No options") until
      the candidate types something, then options are fetched from a
      server. For these we type the target value, wait for the debounced
      server response, then pick the first option that looks like a real
      match.

    Returns True on success.
    """

    state = _open_react_select(page, combo)
    if state == "fail":
        return False

    # Pass 1: if a static listbox is already showing, try to pick from
    # those options directly. (Skip when typeahead-style.)
    if state == "open":
        if _try_pick_listbox_option(page, combo, target):
            return True

    # Pass 2: typeahead with progressively-shorter queries.
    #
    # We try the full target first, then fall back to shorter prefixes.
    # This handles two real-world cases we hit on Greenhouse:
    #
    #   * Static dropdown where the local filter is strict, so e.g.
    #     "Masters Degree" filters to nothing (the option is "Master's
    #     Degree" with an apostrophe). Typing "Mast" still matches.
    #   * Server-backed city search where the full string returns no
    #     results because the user's profile uses an ambiguous name.
    #     A 3-4 character prefix gets the API to return suggestions we
    #     can substring-match against.
    queries: list[str] = [target]
    first_word = target.split()[0] if target.split() else target
    seen = {target.lower()}
    if first_word and first_word.lower() not in seen:
        queries.append(first_word)
        seen.add(first_word.lower())
    short_prefix = (first_word or target)[:4].strip()
    if short_prefix and short_prefix.lower() not in seen:
        queries.append(short_prefix)
        seen.add(short_prefix.lower())

    logger.debug("Typeahead queries for %r: %s", target, queries)

    for idx, query in enumerate(queries):
        # On every retry, re-open the dropdown so the input is focused
        # and any prior listbox is dismissed. The first iteration is
        # already focused from the initial _open_react_select() call.
        if idx > 0:
            _press_escape(page)
            try:
                combo.scroll_into_view_if_needed(timeout=2_000)
                combo.click(timeout=4_000)
            except PlaywrightError as exc:
                logger.debug("Could not re-focus combobox for %r: %s", query, exc)
                continue

        try:
            combo.fill("")
        except PlaywrightError:
            pass
        try:
            combo.press_sequentially(query, delay=25)
        except PlaywrightError as exc:
            logger.debug("Could not type %r into combobox: %s", query, exc)
            continue

        if not _wait_for_listbox_options(page, combo, timeout_ms=3_500):
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    raw_opts = _read_visible_listbox_options(page, combo)
                except PlaywrightError:
                    raw_opts = []
                logger.debug(
                    "No typeahead options appeared after typing %r. "
                    "(visible listbox content: %s)",
                    query,
                    raw_opts,
                )
            else:
                logger.debug(
                    "No typeahead options appeared after typing %r.", query,
                )
            continue

        # We typed ``query`` but still want to pick the option that
        # actually represents ``target`` (e.g. typed "Mast", picked
        # "Master's Degree"). Pass the full target to the matcher.
        if logger.isEnabledFor(logging.DEBUG):
            try:
                opts = _read_visible_listbox_options(page, combo)
                logger.debug(
                    "Listbox options for query %r (matching against %r): %s",
                    query,
                    target,
                    opts,
                )
            except PlaywrightError:
                pass

        if _try_pick_listbox_option(page, combo, target, allow_first=True):
            return True

    # If a static listbox was visible up front, it means this combobox
    # has a fixed option set and our profile value simply doesn't match
    # any of them (e.g. Paradigm's veteran question only has Yes/No, but
    # the EEO profile.veteran_status is a long sentence). Don't try the
    # free-form fallback here -- it'd commit nonsense text. Return False
    # so the caller can fall back to the AI path, which reads the real
    # options and picks one.
    if state == "open":
        _press_escape(page)
        return False

    # Free-form fallback for typeahead-only fields. Some Greenhouse
    # comboboxes (notably the candidate-location field, which uses a
    # Greenhouse-hosted location API that often returns no results in
    # embed / headless sessions) never surface suggestions. The typed
    # text is still kept in the React-Select's internal ``data-value``
    # and may be accepted on submit. We leave ``target`` typed and tab
    # out so React commits the value, then return True so the caller
    # can distinguish free-form vs real match via
    # ``_react_select_has_value``.
    try:
        combo.fill("")
        combo.press_sequentially(target, delay=25)
        page.wait_for_timeout(200)
        combo.press("Tab")
    except PlaywrightError:
        _press_escape(page)
        return False
    logger.debug("No suggestions for %r; left free-form text in combobox.", target)
    return True


def _read_visible_listbox_options(page: Page, combo: Locator) -> list[str]:
    combo_id = combo.get_attribute("id") or ""
    listbox = page.locator(f'[id^="react-select-{combo_id}-listbox"]')
    if listbox.count() == 0:
        listbox = page.locator('[role="listbox"]:visible').first
    else:
        listbox = listbox.first
    options = listbox.locator('[role="option"]')
    out: list[str] = []
    try:
        n = options.count()
    except PlaywrightError:
        n = 0
    for i in range(min(n, 12)):
        try:
            txt = options.nth(i).inner_text(timeout=300).strip()
        except PlaywrightError:
            continue
        if txt:
            out.append(txt)

    # Also surface Google Places .pac-item suggestions so debug logs
    # tell us what's actually on screen for typeaheads like Greenhouse's
    # candidate-location field.
    try:
        pac = page.locator(".pac-container:visible .pac-item")
        m = pac.count()
        for i in range(min(m, 8)):
            try:
                txt = pac.nth(i).inner_text(timeout=300).strip()
            except PlaywrightError:
                continue
            if txt:
                out.append(f"[pac] {txt}")
    except PlaywrightError:
        pass
    return out


def _wait_for_listbox_options(
    page: Page, combo: Locator, *, timeout_ms: int = 2_500
) -> bool:
    """Poll until the dropdown for ``combo`` shows at least one real
    selectable option. Recognises both React-Select listboxes and the
    Google Places ``.pac-container`` overlay that Greenhouse's
    ``candidate-location`` field renders."""

    combo_id = combo.get_attribute("id") or ""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            listbox = page.locator(f'[id^="react-select-{combo_id}-listbox"]')
            if listbox.count() == 0:
                listbox = page.locator('[role="listbox"]:visible').first
            else:
                listbox = listbox.first
            options = listbox.locator('[role="option"]')
            count = options.count()
            for i in range(count):
                try:
                    text = options.nth(i).inner_text(timeout=300).strip()
                except PlaywrightError:
                    continue
                if not text:
                    continue
                if any(noise in text.lower() for noise in _NOISE_OPTION_LABELS if noise):
                    continue
                return True
        except PlaywrightError:
            pass

        # Google Places Autocomplete (Greenhouse's "Location (City)" uses
        # this). Items live in a body-portalled .pac-container, not the
        # React-Select listbox.
        try:
            pac_items = page.locator('.pac-container:visible .pac-item')
            if pac_items.count() > 0:
                return True
        except PlaywrightError:
            pass

        page.wait_for_timeout(150)
    return False


# Strings that indicate a placeholder / loading / "no results" option
# rather than an actual selectable answer. Used by typeahead fallback so
# we don't accidentally click "No options" or a spinner row.
_NOISE_OPTION_LABELS = (
    "",
    "no options",
    "no options found",
    "loading...",
    "loading",
    "type to search",
    "start typing",
    "type at least",
)


def _try_pick_listbox_option(
    page: Page,
    combo: Locator,
    target: str,
    *,
    allow_first: bool = False,
) -> bool:
    """Locate the listbox associated with ``combo`` and click the option
    that best matches ``target``. Returns True on success.

    When ``allow_first`` is True, the top non-noise option is clicked even
    if it doesn't substring-match the target; this is desirable for
    typeahead (the user already constrained the result by typing) but not
    for static lists (where we don't want to click the wrong gender etc.).
    """

    # Google Places Autocomplete short-circuit: if a visible
    # .pac-container is showing, click its first matching .pac-item.
    if _try_pick_pac_item(page, target, allow_first=allow_first):
        return True

    try:
        combo_id = combo.get_attribute("id") or ""
        listbox = page.locator(f'[id^="react-select-{combo_id}-listbox"]')
        if listbox.count() == 0:
            listbox = page.locator('[role="listbox"]:visible').first
        else:
            listbox = listbox.first

        options = listbox.locator('[role="option"]')
        count = options.count()
        if count == 0:
            return False

        target_lc = target.lower().strip()
        target_norm = _normalize_match(target)

        def _option_text(i: int) -> str:
            try:
                return options.nth(i).inner_text(timeout=500).strip()
            except PlaywrightError:
                return ""

        # 1. Exact (case-insensitive) match, with punctuation-normalized
        #    fallback so e.g. "Masters Degree" matches "Master's Degree".
        for i in range(count):
            txt = _option_text(i)
            if txt.lower() == target_lc:
                return _click_option(options.nth(i))
            if _normalize_match(txt) == target_norm:
                return _click_option(options.nth(i))

        # 2. WORD-BOUNDARY match. Prefer options where the target
        #    appears as a whole word/phrase rather than embedded
        #    mid-word. Without this, target "male" matches BOTH
        #    "Male / Man" AND "Female / Woman" (because "Female"
        #    contains "male"), and we end up clicking the alphabetically
        #    first option which is the wrong gender. ``\b`` only matches
        #    on word boundaries so "male" no longer matches inside
        #    "female".
        target_word_re: re.Pattern[str] | None = None
        if target_norm:
            try:
                target_word_re = re.compile(
                    rf"\b{re.escape(target_norm)}\b"
                )
            except re.error:
                target_word_re = None
        if target_word_re is not None:
            for i in range(count):
                txt = _option_text(i)
                if not txt:
                    continue
                txt_norm = _normalize_match(txt)
                if target_word_re.search(txt_norm):
                    return _click_option(options.nth(i))

        # 3. Substring match (either direction, on normalized text).
        #    Kept as a fallback for cases where the target straddles
        #    punctuation in ways the word-boundary check misses (e.g.
        #    typed "Westfield" against "Westfield, NJ, USA" already
        #    matches in step 2, but odd punctuation can defeat \b).
        for i in range(count):
            txt = _option_text(i)
            if not txt:
                continue
            txt_norm = _normalize_match(txt)
            if target_norm in txt_norm or txt_norm in target_norm:
                return _click_option(options.nth(i))

        # 4. First non-noise option (typeahead only).
        if allow_first:
            for i in range(count):
                txt = _option_text(i).lower()
                if any(n in txt for n in _NOISE_OPTION_LABELS):
                    continue
                if not txt:
                    continue
                return _click_option(options.nth(i))

        return False
    except PlaywrightError as exc:
        logger.debug("listbox interaction failed: %s", exc)
        return False


def _try_pick_pac_item(
    page: Page, target: str, *, allow_first: bool = False
) -> bool:
    """Click a Google Places Autocomplete suggestion that matches
    ``target``. Returns True on success, False if no .pac-container is
    visible or no matching item could be clicked."""

    try:
        container = page.locator(".pac-container:visible")
        if container.count() == 0:
            return False
        items = container.first.locator(".pac-item")
        n = items.count()
        if n == 0:
            return False

        target_norm = _normalize_match(target)

        # Prefer items whose normalized text contains the target (e.g.
        # "Westfield, NJ, USA" contains "westfield").
        for i in range(n):
            try:
                txt = items.nth(i).inner_text(timeout=300).strip()
            except PlaywrightError:
                continue
            if not txt:
                continue
            if target_norm in _normalize_match(txt):
                try:
                    items.nth(i).click(timeout=2_000)
                    return True
                except PlaywrightError:
                    continue

        if allow_first:
            try:
                items.first.click(timeout=2_000)
                return True
            except PlaywrightError:
                return False
        return False
    except PlaywrightError:
        return False


def _normalize_match(text: str) -> str:
    """Lower-case + strip punctuation so e.g. ``Master's Degree`` and
    ``Masters Degree`` match. Whitespace is collapsed to single spaces.

    Note: apostrophes are deleted (not replaced with a space) so
    ``master's`` becomes ``masters`` rather than ``master s``. Other
    punctuation becomes a space so e.g. ``M.B.A.`` becomes ``m b a``."""

    s = text.lower()
    for ch in ("'", "\u2019", "\u2018", "\u02bc", "`"):
        s = s.replace(ch, "")
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in s)
    return " ".join(cleaned.split())


def _click_option(opt: Locator) -> bool:
    try:
        opt.scroll_into_view_if_needed(timeout=1_000)
        opt.click(timeout=3_000)
        return True
    except PlaywrightError as exc:
        logger.warning("Could not click option in React-Select: %s", exc)
        return False


def _press_escape(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        time.sleep(0.1)
    except PlaywrightError:
        pass


def _react_select_picked_count(page: Page, combo: Locator) -> int:
    """Count how many options have been picked in a multi-select
    React-Select. Each picked option is rendered as a
    ``.select__multi-value`` chip inside the same control container."""

    try:
        container = combo.locator(
            'xpath=ancestor::*[contains(@class, "select__control")]'
        ).first
        if container.count() == 0:
            return 0
        return container.locator(".select__multi-value").count()
    except PlaywrightError:
        return 0


def _select_react_multi_options(
    page: Page,
    combo: Locator,
    targets: list[str],
) -> int:
    """Pick every ``targets`` value in a multi-select React-Select.

    Strategy: for each target, run the existing single-pick typeahead
    (``_select_react_option``). React-Select multi-mode keeps the
    listbox open after a click and clears the input automatically, so
    we can usually loop without re-clicking the combobox -- but as a
    safety net we re-focus on each iteration in case the menu closed.

    Returns the number of *new* chips that appeared in the control
    after the loop, so the caller knows how many options were actually
    committed (vs. requested)."""

    if not targets:
        return 0

    before = _react_select_picked_count(page, combo)
    for tgt in targets:
        # Re-focus the combobox -- some forms close the listbox between
        # picks and the typeahead path needs an interactive input.
        try:
            combo.click(timeout=2_000)
        except PlaywrightError:
            try:
                combo.focus(timeout=1_500)
            except PlaywrightError:
                pass
        # Reuse the single-pick path; it handles typeahead, normalization,
        # and the static-listbox case all in one. We don't care about
        # its return value here -- we just measure chip count growth.
        try:
            _select_react_option(page, combo, tgt)
        except PlaywrightError as exc:
            logger.debug("Multi-pick failed for %r: %s", tgt, exc)
            continue

    _press_escape(page)
    after = _react_select_picked_count(page, combo)
    return max(0, after - before)


# ---------------------------------------------------------------------------
# Radio button groups
# ---------------------------------------------------------------------------


def _fill_radio_groups(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    """Find every <input type=radio> on the page, group by ``name``, and
    pick the option that matches the candidate's profile (or, failing
    that, the AI's choice).

    Greenhouse often hides the actual ``<input>`` visually with CSS
    (``opacity:0``) so we don't filter on ``:visible`` -- we instead
    check whether the radio's surrounding label/wrapper is visible
    before interacting. ``Locator.check()`` knows how to forward the
    click to the visible label automatically.
    """

    radios = page.locator('input[type="radio"]')
    try:
        count = radios.count()
    except PlaywrightError:
        count = 0
    if count == 0:
        return

    groups: dict[str, list[Locator]] = {}
    for i in range(count):
        r = radios.nth(i)
        try:
            name = (r.get_attribute("name") or "").strip()
        except PlaywrightError:
            continue
        if not name:
            # Anonymous radio (rare). Use a synthetic key so we still
            # process it as a single-member group.
            name = f"__anon_radio_{i}"
        if not _is_radio_group_visible(page, r):
            continue
        groups.setdefault(name, []).append(r)

    for group_name, members in groups.items():
        if not members:
            continue

        # Skip pre-selected groups.
        try:
            if any(_safe_is_checked(m) for m in members):
                logger.debug("Skipping pre-selected radio group %r.", group_name)
                continue
        except PlaywrightError:
            continue

        question, label = _radio_group_label_and_question(page, members)
        options: list[str] = []
        radio_by_label: dict[str, Locator] = {}
        for m in members:
            opt_text = _radio_or_checkbox_label_text(page, m)
            if not opt_text:
                continue
            if opt_text not in radio_by_label:
                options.append(opt_text)
                radio_by_label[opt_text] = m

        if not options:
            logger.debug("Radio group %r has no labelled options; skipping.", group_name)
            continue

        chosen = _match_option_label(label, options, profile)
        source = "profile"
        if not chosen and ai is not None:
            chosen = ai.pick_option(
                QuestionContext(label=label, question=question, options=tuple(options))
            )
            source = "AI" if chosen else "AI (no match)"

        if not chosen:
            result.skipped.append(f"{label} (radio: no answer)")
            continue

        radio = radio_by_label.get(chosen) or radio_by_label.get(_canon(chosen))
        if radio is None:
            radio = next(
                (
                    r for k, r in radio_by_label.items()
                    if _canon(k) == _canon(chosen)
                ),
                None,
            )
        if radio is None:
            result.skipped.append(f"{label} (radio: option {chosen!r} not in DOM)")
            continue

        if _check_radio_or_checkbox(page, radio):
            tag = "" if source == "profile" else f" ({source})"
            logger.info("Radio %r = %r%s", label, chosen, tag)
            result.filled.append(f"{label}{tag}")
        else:
            result.failed.append(f"{label} (radio click failed)")


# ---------------------------------------------------------------------------
# Checkboxes (single acknowledgements + multi-select groups)
# ---------------------------------------------------------------------------


def _fill_checkbox_fields(
    page: Page,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    """Handle three flavours of checkboxes Greenhouse uses:

    * **Single acknowledgement** -- e.g. "I agree to the Privacy Notice".
      We ask the AI whether it should be checked (yes/no). Required
      acknowledgements are nudged toward yes via the question text.
    * **Multi-select group** -- e.g. "Which of the following apply?".
      Several checkboxes share the same ``name`` (or live in the same
      fieldset). The AI returns the subset that applies and we tick
      each.
    * **Self-ID style multi-checkbox** -- some forms put race / gender
      as a checkbox set instead of a dropdown. We try the profile
      mapping first and only fall back to the AI when no match.
    """

    boxes = page.locator('input[type="checkbox"]')
    try:
        count = boxes.count()
    except PlaywrightError:
        count = 0
    if count == 0:
        return

    # Group by name. Anonymous (unnamed) checkboxes are treated as their
    # own one-element group so we still process them.
    groups: dict[str, list[Locator]] = {}
    for i in range(count):
        b = boxes.nth(i)
        try:
            name = (b.get_attribute("name") or "").strip()
        except PlaywrightError:
            continue
        if not _is_radio_group_visible(page, b):
            continue
        key = name or f"__anon_checkbox_{i}"
        groups.setdefault(key, []).append(b)

    for group_name, members in groups.items():
        if not members:
            continue

        if len(members) == 1:
            _process_single_checkbox(page, members[0], profile, ai, result)
        else:
            _process_checkbox_group(page, members, profile, ai, result, group_name)


def _process_single_checkbox(
    page: Page,
    box: Locator,
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
) -> None:
    label = _radio_or_checkbox_label_text(page, box) or _get_label(page, box) or "(unlabelled checkbox)"

    if _safe_is_checked(box):
        logger.debug("Skipping pre-checked checkbox %r.", label)
        return

    # Required acknowledgements: just check them (the candidate is, by
    # definition, agreeing to the application's terms by submitting).
    is_required = False
    try:
        is_required = (
            box.get_attribute("required") is not None
            or (box.get_attribute("aria-required") or "").lower() == "true"
        )
    except PlaywrightError:
        pass

    if is_required:
        if _check_radio_or_checkbox(page, box):
            logger.info("Checked required checkbox %r.", label)
            result.filled.append(f"{label} (required)")
        else:
            result.failed.append(f"{label} (could not check)")
        return

    # Optional checkbox -- only check if AI thinks we should.
    if ai is None:
        result.skipped.append(f"{label} (optional checkbox, AI disabled)")
        return

    question = _question_text_for_label(page, box, label)
    decision = ai.pick_option(
        QuestionContext(
            label=label,
            question=(
                "Should this checkbox be checked? Reply with exactly "
                '"Yes" or "No". Question:\n' + question
            ),
            options=("Yes", "No"),
        )
    )
    if decision and decision.lower() == "yes":
        if _check_radio_or_checkbox(page, box):
            logger.info("AI-checked optional checkbox %r.", label)
            result.filled.append(f"{label} (AI checked)")
        else:
            result.failed.append(f"{label} (AI said yes but click failed)")
    else:
        logger.debug("Leaving optional checkbox %r unchecked (AI=%r).", label, decision)
        result.skipped.append(f"{label} (AI: leave unchecked)")


def _process_checkbox_group(
    page: Page,
    members: list[Locator],
    profile: ApplicantProfile,
    ai: AIAnswerer | None,
    result: AutobidResult,
    group_name: str,
) -> None:
    question, label = _radio_group_label_and_question(page, members)

    options: list[str] = []
    box_by_label: dict[str, Locator] = {}
    for m in members:
        opt_text = _radio_or_checkbox_label_text(page, m)
        if not opt_text:
            continue
        if opt_text not in box_by_label:
            options.append(opt_text)
            box_by_label[opt_text] = m

    if not options:
        return

    # Skip if any are already checked -- the user (or a previous run)
    # has answered.
    if any(_safe_is_checked(m) for m in members):
        logger.debug("Skipping pre-checked checkbox group %r.", group_name)
        return

    # Try profile match first (e.g. race / hispanic-as-checkboxes).
    profile_pick = _match_option_label(label, options, profile)
    chosen_labels: list[str] = []
    if profile_pick:
        chosen_labels = [profile_pick]
        source = "profile"
    elif ai is not None:
        picks = ai.pick_multi(
            QuestionContext(label=label, question=question, options=tuple(options))
        )
        chosen_labels = list(picks)
        source = "AI"
    else:
        result.skipped.append(f"{label} (multi-checkbox: AI disabled)")
        return

    if not chosen_labels:
        logger.debug("No checkbox options selected for %r.", label)
        result.skipped.append(f"{label} (multi-checkbox: nothing selected)")
        return

    checked: list[str] = []
    failed: list[str] = []
    for opt in chosen_labels:
        box = box_by_label.get(opt) or next(
            (b for k, b in box_by_label.items() if _canon(k) == _canon(opt)),
            None,
        )
        if box is None:
            failed.append(opt)
            continue
        if _check_radio_or_checkbox(page, box):
            checked.append(opt)
        else:
            failed.append(opt)

    tag = "" if source == "profile" else f" ({source})"
    if checked:
        logger.info("Checkbox group %r = %s%s", label, checked, tag)
        result.filled.append(f"{label} = {checked}{tag}")
    if failed:
        result.failed.append(f"{label} (could not check: {failed})")


# ---------------------------------------------------------------------------
# Radio / checkbox shared helpers
# ---------------------------------------------------------------------------


def _is_radio_group_visible(page: Page, el: Locator) -> bool:
    """The radio/checkbox <input> is often visually hidden, but its
    associated label or fieldset wrapper is what the user actually
    sees. We treat the control as "available" if any of its visible
    surrogates are visible."""

    try:
        if el.is_visible():
            return True
    except PlaywrightError:
        pass

    try:
        el_id = (el.get_attribute("id") or "").strip()
    except PlaywrightError:
        el_id = ""

    if el_id:
        try:
            lbl = page.locator(f'label[for="{_css_escape(el_id)}"]').first
            if lbl.count() > 0 and lbl.is_visible():
                return True
        except PlaywrightError:
            pass

    try:
        ancestor_label = el.locator("xpath=ancestor::label[1]").first
        if ancestor_label.count() > 0 and ancestor_label.is_visible():
            return True
    except PlaywrightError:
        pass

    try:
        wrapper = el.locator(
            "xpath=ancestor::*[contains(@class,'application--field')"
            " or contains(@class,'application-question')"
            " or self::fieldset][1]"
        ).first
        if wrapper.count() > 0 and wrapper.is_visible():
            return True
    except PlaywrightError:
        pass

    return False


def _radio_or_checkbox_label_text(page: Page, el: Locator) -> str:
    """Return the *option label* text for a single radio/checkbox.
    Different from ``_get_label`` which returns the question label."""

    try:
        el_id = (el.get_attribute("id") or "").strip()
    except PlaywrightError:
        el_id = ""

    if el_id:
        try:
            lbl = page.locator(f'label[for="{_css_escape(el_id)}"]').first
            if lbl.count() > 0:
                txt = (lbl.inner_text(timeout=500) or "").strip()
                if txt:
                    return _trim_required_marker(txt)
        except PlaywrightError:
            pass

    try:
        ancestor_label = el.locator("xpath=ancestor::label[1]").first
        if ancestor_label.count() > 0:
            txt = (ancestor_label.inner_text(timeout=500) or "").strip()
            if txt:
                return _trim_required_marker(txt)
    except PlaywrightError:
        pass

    try:
        aria = (el.get_attribute("aria-label") or "").strip()
    except PlaywrightError:
        aria = ""
    if aria:
        return aria

    try:
        value = (el.get_attribute("value") or "").strip()
    except PlaywrightError:
        value = ""
    return value


def _radio_group_label_and_question(
    page: Page, members: list[Locator]
) -> tuple[str, str]:
    """Compute (question_text, short_label) for a group of radios or
    checkboxes that share a question (typically a fieldset or
    application-question wrapper)."""

    if not members:
        return "", "(empty group)"

    el = members[0]

    # Prefer the closest fieldset legend.
    try:
        legend = el.locator("xpath=ancestor::fieldset[1]/legend").first
        if legend.count() > 0:
            txt = (legend.inner_text(timeout=500) or "").strip()
            if txt:
                cleaned = _trim_required_marker(txt)
                return cleaned, cleaned.split("\n")[0][:120]
    except PlaywrightError:
        pass

    # Otherwise the nearest application-question wrapper.
    try:
        wrapper = el.locator(
            "xpath=ancestor::*[contains(@class,'application--field')"
            " or contains(@class,'application-question')][1]"
        ).first
        if wrapper.count() > 0:
            txt = (wrapper.inner_text(timeout=500) or "").strip()
            if txt:
                cleaned = _trim_required_marker(txt)
                return cleaned, cleaned.split("\n")[0][:120]
    except PlaywrightError:
        pass

    fallback = _get_label(page, el) or "(unlabelled group)"
    return fallback, fallback


def _safe_is_checked(el: Locator) -> bool:
    try:
        return el.is_checked(timeout=500)
    except PlaywrightError:
        return False


def _check_radio_or_checkbox(page: Page, el: Locator) -> bool:
    """Tick a radio / checkbox even when the input itself is visually
    hidden (Greenhouse styles them via the surrounding label).

    Strategy:
      1. ``Locator.check(force=False)`` -- works when the input is
         visible or its label is hittable.
      2. Click the associated ``<label for=id>`` directly.
      3. Click the closest ancestor ``<label>``.
      4. ``Locator.check(force=True)`` as a last resort.
    """

    try:
        el.scroll_into_view_if_needed(timeout=1_500)
    except PlaywrightError:
        pass

    try:
        el.check(timeout=2_000)
        return True
    except PlaywrightError:
        pass

    el_id = ""
    try:
        el_id = (el.get_attribute("id") or "").strip()
    except PlaywrightError:
        pass

    if el_id:
        try:
            lbl = page.locator(f'label[for="{_css_escape(el_id)}"]').first
            if lbl.count() > 0:
                lbl.click(timeout=2_000)
                return _safe_is_checked(el)
        except PlaywrightError:
            pass

    try:
        ancestor_label = el.locator("xpath=ancestor::label[1]").first
        if ancestor_label.count() > 0:
            ancestor_label.click(timeout=2_000)
            return _safe_is_checked(el)
    except PlaywrightError:
        pass

    try:
        el.check(force=True, timeout=2_000)
        return _safe_is_checked(el)
    except PlaywrightError:
        return False


def _match_option_label(
    label: str, options: list[str], profile: ApplicantProfile
) -> str | None:
    """If ``label`` corresponds to a known profile field (gender, race,
    veteran status, etc.) and one of ``options`` matches the profile's
    value, return that option label; otherwise ``None``."""

    target = _match_select_value(label, profile)
    if not target:
        return None
    target_norm = _normalize_match(target)
    for opt in options:
        if _normalize_match(opt) == target_norm:
            return opt
    for opt in options:
        opt_n = _normalize_match(opt)
        if target_norm in opt_n or opt_n in target_norm:
            return opt
    return None


def _canon(s: str) -> str:
    return _normalize_match(s or "")


# ---------------------------------------------------------------------------
# Resume upload
# ---------------------------------------------------------------------------


def _upload_resume(page: Page, path: str) -> None:
    """Upload the resume file to a Greenhouse application form.

    The non-obvious bit: Playwright's ``set_input_files`` correctly
    populates ``input.files`` and fires native ``input``/``change`` events,
    but Greenhouse's React form *still does not update* unless we also
    reset React's internal ``_valueTracker``. React's synthetic onChange
    short-circuits when ``tracker.getValue() === inp.value`` (both empty
    initially, since file inputs always read as empty for ``inp.value``
    until they're "real-user touched"), so React's state update never runs
    and the post-upload UI is never rendered. Resetting the tracker and
    re-dispatching ``change`` forces React to re-evaluate.

    React also needs to be fully hydrated before this trick works, so we
    explicitly wait for ``networkidle`` and a short settle delay before
    interacting.

    If for some reason the tracker reset doesn't take effect, we fall
    back to the Attach button + file chooser flow.
    """

    file_inputs_total = page.locator('input[type="file"]').count()
    logger.debug(
        "Resume upload: %d <input type=file> element(s) on page.",
        file_inputs_total,
    )

    # Wait for the page to fully settle so React has hydrated and bound
    # its onChange handlers before we touch the file input.
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(400)

    # Strategy 1: set_input_files + React _valueTracker reset.
    resume_input = _find_resume_file_input(page)
    if resume_input is not None:
        try:
            resume_input.set_input_files(path)
            _trigger_react_file_change(page, "input#resume")
            if _wait_for_resume_attached(page, path, timeout_ms=6_000):
                logger.info(
                    "Resume attached via set_input_files + tracker reset."
                )
                # Give Greenhouse's resume parser a beat to back-fill
                # First/Last/Email so our verify-and-refill pass picks up
                # its overrides.
                page.wait_for_timeout(800)
                return
            logger.warning(
                "set_input_files + tracker reset did not produce a "
                "post-upload indicator; trying Attach button fallback."
            )
        except PlaywrightError as exc:
            logger.warning(
                "Hidden file input upload failed: %s; trying Attach button.",
                exc,
            )

    # Strategy 2: click the visible Attach button and intercept the file
    # chooser. Used when Strategy 1 failed (e.g., for Greenhouse variants
    # where the file input doesn't react to programmatic changes).
    attach_btn = _find_resume_attach_button(page)
    if attach_btn is None:
        raise RuntimeError(
            "Could not locate a Resume/CV file input or Attach button."
        )
    try:
        attach_btn.scroll_into_view_if_needed(timeout=2_000)
    except PlaywrightError:
        pass
    try:
        with page.expect_file_chooser(timeout=5_000) as fc_info:
            attach_btn.click()
        fc_info.value.set_files(path)
        _trigger_react_file_change(page, "input#resume")
    except PlaywrightTimeoutError:
        # No chooser fired; final ditch: set_input_files again on whatever
        # file input we can still find.
        last_resort = _find_resume_file_input(page)
        if last_resort is None:
            raise RuntimeError(
                "Attach button did not open a file chooser, and no file "
                "input remains for a final set_input_files attempt."
            )
        last_resort.set_input_files(path)
        _trigger_react_file_change(page, "input#resume")

    if not _wait_for_resume_attached(page, path, timeout_ms=10_000):
        raise RuntimeError(
            "Resume upload completed but no post-upload indicator "
            "appeared in the Resume/CV section."
        )
    logger.info("Resume attached via Attach button fallback.")
    page.wait_for_timeout(800)


def _trigger_react_file_change(page: Page, input_selector: str) -> None:
    """Force React to re-run its onChange after a programmatic file set.

    React tracks input values via an internal ``_valueTracker``; if we set
    ``input.files`` programmatically (Playwright does this natively), the
    tracker still believes the value is unchanged and React's synthetic
    onChange short-circuits. Resetting the tracker to '' then dispatching
    a bubbling ``change`` event makes React see a fresh diff and run the
    handler that actually attaches the file in form state.
    """

    try:
        result = page.evaluate(
            """
            (selector) => {
              const inp = document.querySelector(selector);
              if (!inp) return 'no-input';
              const tracker = inp._valueTracker;
              if (tracker) tracker.setValue('');
              inp.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
              inp.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
              return 'ok';
            }
            """,
            input_selector,
        )
        logger.debug("React tracker reset for %s: %s", input_selector, result)
    except PlaywrightError as exc:
        logger.warning("React tracker reset failed for %s: %s", input_selector, exc)


def _find_resume_file_input(page: Page) -> Locator | None:
    """Return the hidden ``<input type="file">`` for the Resume/CV section.

    Strategy:
      1. Scope to the section whose label/heading is "Resume/CV" and look
         for an ``input[type=file]`` inside or right after it.
      2. Fall back to the very first ``input[type=file]`` on the page
         (which on Greenhouse forms is virtually always the resume one,
         since Cover Letter -- if present -- comes second).
    """

    # Greenhouse 2024+ forms wrap each attachment in a `<div data-source>`
    # (or similar) container whose label text starts with "Resume" or
    # "Resume/CV". Look upward from a "Resume/CV" label/heading to the
    # nearest enclosing block, then look for a file input within.
    candidates = (
        # Modern Greenhouse: container holding both the label text and inputs.
        'div:has(:text("Resume/CV")) input[type="file"]',
        'div:has(label:has-text("Resume")) input[type="file"]',
        'fieldset:has(:text("Resume")) input[type="file"]',
        'section:has(:text("Resume")) input[type="file"]',
    )
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                logger.debug("Resume file input matched via selector: %s", sel)
                return loc
        except PlaywrightError:
            continue

    fallback = page.locator('input[type="file"]')
    if fallback.count() > 0:
        logger.debug(
            "Falling back to first input[type=file] on page (count=%d).",
            fallback.count(),
        )
        return fallback.first
    return None


def _find_resume_attach_button(page: Page) -> Locator | None:
    """Return the visible "Attach" button inside the Resume/CV section."""

    candidates = (
        'div:has(:text("Resume/CV")) >> button:has-text("Attach")',
        'div:has(label:has-text("Resume")) >> button:has-text("Attach")',
        'fieldset:has(:text("Resume")) >> button:has-text("Attach")',
        'section:has(:text("Resume")) >> button:has-text("Attach")',
    )
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                logger.debug("Attach button matched via selector: %s", sel)
                return loc
        except PlaywrightError:
            continue

    btns = page.locator('button:has-text("Attach"), button:has-text("Upload")')
    if btns.count() > 0:
        logger.debug(
            "Falling back to first Attach/Upload button on page (count=%d).",
            btns.count(),
        )
        return btns.first
    return None


def _wait_for_resume_attached(
    page: Page, file_path: str, *, timeout_ms: int
) -> bool:
    """Poll for evidence that the resume actually attached.

    On Greenhouse, a successful upload replaces the
    ``<div class="button-container">…Attach/Dropbox/Enter manually…</div>``
    block with a ``<div class="file-upload__filename">`` containing a file
    icon, the filename in a ``<p>``, and a "Remove file" icon-button. We
    accept any of those as confirmation, plus a few generic fallbacks for
    other Greenhouse variants.
    """

    file_name = Path(file_path).name
    deadline = time.time() + (timeout_ms / 1000)

    indicator_selectors = (
        # Greenhouse 2024+ post-upload UI inside the resume section.
        f'.file-upload:has(#upload-label-resume) .file-upload__filename:has-text("{file_name}")',
        '.file-upload:has(#upload-label-resume) button[aria-label="Remove file"]',
        '.file-upload:has(#upload-label-resume) .file-upload__filename',
        # Generic post-upload indicators.
        f'text="{file_name}"',
        'button:has-text("Remove")',
        'button:has-text("Replace")',
        'button[aria-label*="remove" i]',
    )

    while time.time() < deadline:
        for sel in indicator_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=200):
                    logger.debug(
                        "Resume upload confirmed via selector: %s", sel
                    )
                    return True
            except PlaywrightError:
                continue
        time.sleep(0.4)

    return False


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


_SUBMIT_BUTTON_SELECTOR = (
    'button[type="submit"]:has-text("Submit"), '
    'button:has-text("Submit Application"), '
    'button:has-text("Submit application")'
)

# Inputs that look like a Greenhouse "security code" field that appears
# AFTER the first submit click. Greenhouse boards use two distinct
# layouts for this prompt:
#
# 1. A single text input (``id="security_code"`` etc).
# 2. A row of N single-character inputs nested inside
#    ``<fieldset id="email-verification">``, each with
#    ``id="security-input-0"`` ... ``security-input-N-1`` (note the
#    DASHES, not underscores).
#
# Both shapes need to be detected so ``_wait_for_submit_outcome`` can
# return ``needs_code``. The actual code-typing logic in
# :func:`_fill_security_code` then dispatches to the right strategy.
_SECURITY_CODE_SELECTOR = (
    'input[id*="security_code" i], '
    'input[name*="security_code" i], '
    'input[id*="verification_code" i], '
    'input[name*="verification_code" i], '
    'input[aria-label*="security code" i], '
    'input[aria-label*="verification code" i], '
    'input[placeholder*="security code" i], '
    'input[placeholder*="verification code" i], '
    'fieldset[id*="email-verification" i], '
    'fieldset[id*="email_verification" i], '
    'input[id^="security-input-" i], '
    'input[id^="verification-input-" i]'
)

# CSS for the per-character input row. Used by :func:`_fill_security_code`
# to detect "split" code prompts and route to the keyboard-typing path.
_PER_CHAR_CODE_SELECTOR = (
    'input[id^="security-input-" i], '
    'input[id^="verification-input-" i]'
)

# Greenhouse embed forms (``/embed/job_app``) often confirm submission
# *inline* -- the URL stays the same but the form is replaced with a
# "Thank you for applying" panel. We treat any of these phrases (in
# visible page text) as a successful submit.
_CONFIRMATION_TEXT_REGEX = re.compile(
    r"thank you for applying"
    r"|thanks for applying"
    r"|application (?:was|has been) (?:received|submitted|sent)"
    r"|your application (?:was|has been) (?:received|submitted|sent)"
    r"|we(?:'ve| have) received your application"
    r"|application (?:complete|submitted successfully)"
    r"|successfully submitted",
    re.I,
)

# Selectors that signal a *visible* form-validation error after submit.
_FORM_ERROR_SELECTOR = (
    '[role="alert"]:visible, '
    '.error:visible, '
    '.field_error:visible, '
    '.application-form-error:visible, '
    '.input--error:visible, '
    '[aria-invalid="true"]:visible'
)

# Maximum time we wait between "submit clicked" and "outcome resolved".
_SUBMIT_OUTCOME_TIMEOUT_MS = 30_000


def _submit_application(
    page: Page, result: AutobidResult, *, action_timeout_ms: int
) -> None:
    """Click ``Submit application`` and resolve one of three outcomes.

    1. **Confirmed** -- URL changes to ``/confirmation``. Done.
    2. **Needs code** -- a security-code field appears. We fetch the
       code from Gmail, paste it, click submit again, and re-resolve.
    3. **Error** -- visible form errors OR no change at all. The user
       is alerted by the test runner so they can fix it manually.
    """

    submit_clicked_at = _dt.datetime.now(_dt.timezone.utc)
    try:
        _click_submit_button(page, action_timeout_ms=action_timeout_ms)
    except Exception as exc:
        logger.error("Submit button click failed: %s", exc)
        result.error = f"Submit failed: {exc}"
        result.submission_status = "no_change"
        result.submission_errors.append(str(exc))
        return

    outcome = _wait_for_submit_outcome(page, timeout_ms=_SUBMIT_OUTCOME_TIMEOUT_MS)

    if outcome == "confirmed":
        result.submitted = True
        result.submission_status = "confirmed"
        logger.info("Application submitted (confirmation page: %s)", page.url)
        return

    if outcome == "needs_code":
        logger.info(
            "Greenhouse asked for an email verification code. "
            "Polling Gmail for the code email..."
        )
        ok = _handle_security_code_prompt(
            page,
            sent_after=submit_clicked_at,
            result=result,
            action_timeout_ms=action_timeout_ms,
        )
        if not ok:
            return
        # Some Greenhouse code-prompts auto-submit when the last
        # character lands (especially the per-char ``security-input-N``
        # layout). Give the page 2s to react first; if it already
        # confirmed itself we don't need to click submit again.
        time.sleep(2.0)
        early = _wait_for_submit_outcome(page, timeout_ms=2_000)
        if early == "confirmed":
            result.submitted = True
            result.submission_status = "verified"
            logger.info(
                "Application verified + submitted (auto-submit after "
                "code entry; confirmation: %s)",
                page.url,
            )
            return
        # Otherwise the form still expects an explicit re-submit click.
        try:
            _click_submit_button(page, action_timeout_ms=action_timeout_ms)
        except Exception as exc:
            # If the submit button is no longer present, the form
            # likely already moved past us; let one more outcome poll
            # decide before reporting failure.
            logger.warning(
                "Re-submit click failed (%s); checking final outcome anyway.",
                exc,
            )
            late = _wait_for_submit_outcome(page, timeout_ms=5_000)
            if late == "confirmed":
                result.submitted = True
                result.submission_status = "verified"
                logger.info(
                    "Application verified + submitted (no re-click "
                    "needed; confirmation: %s)",
                    page.url,
                )
                return
            result.error = f"Re-submit failed: {exc}"
            result.submission_status = "needs_code_failed"
            result.submission_errors.append(str(exc))
            return
        # IMPORTANT: the code-verification POST takes 1-3 seconds; the
        # email-verification fieldset stays visible in the DOM during
        # that window. Without this sleep ``_wait_for_submit_outcome``
        # would see the still-visible code prompt on its very first
        # poll iteration and return ``needs_code`` before the server
        # had a chance to respond.
        time.sleep(3.0)
        second = _wait_for_submit_outcome(page, timeout_ms=_SUBMIT_OUTCOME_TIMEOUT_MS)
        if second == "confirmed":
            result.submitted = True
            result.submission_status = "verified"
            logger.info(
                "Application verified + submitted (confirmation page: %s)",
                page.url,
            )
            return
        if second == "needs_code":
            errs = ["Greenhouse asked for the security code a second time."]
        else:
            errs = _collect_form_errors(page) or [
                "No confirmation page after re-submit; check the form manually."
            ]
        for e in errs:
            logger.error("Re-submit issue: %s", e)
            result.submission_errors.append(e)
        result.submission_status = "needs_code_failed"
        result.error = "; ".join(errs)
        return

    # outcome == "form_error" or "no_change"
    errs = _collect_form_errors(page)
    if errs:
        result.submission_status = "form_error"
        for e in errs:
            logger.error("Form error: %s", e)
            result.submission_errors.append(e)
        result.error = "Form errors after submit: " + "; ".join(errs)
    else:
        result.submission_status = "no_change"
        result.submission_errors.append(
            "Submit click had no visible effect (no confirmation, no code "
            "prompt, no error message). Form may have failed silently."
        )
        result.error = result.submission_errors[-1]
        logger.error(result.submission_errors[-1])


def _click_submit_button(page: Page, *, action_timeout_ms: int) -> None:
    submit = page.locator(_SUBMIT_BUTTON_SELECTOR).first
    submit.wait_for(state="visible", timeout=action_timeout_ms)
    submit.scroll_into_view_if_needed(timeout=2_000)
    submit.click(timeout=action_timeout_ms)


def _wait_for_submit_outcome(page: Page, *, timeout_ms: int) -> str:
    """Poll the page for one of: ``confirmed`` / ``needs_code`` /
    ``form_error`` / ``no_change``."""

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    code_locator = page.locator(_SECURITY_CODE_SELECTOR)
    error_locator = page.locator(_FORM_ERROR_SELECTOR)
    while True:
        # 1. Confirmation URL?
        try:
            current_url = page.url or ""
        except PlaywrightError:
            current_url = ""
        if "/confirmation" in current_url:
            return "confirmed"
        # 2. Visible code-prompt?
        try:
            for i in range(min(code_locator.count(), 5)):
                el = code_locator.nth(i)
                if el.is_visible():
                    return "needs_code"
        except PlaywrightError:
            pass
        # 3. Visible form errors?
        try:
            if error_locator.count() > 0 and error_locator.first.is_visible():
                return "form_error"
        except PlaywrightError:
            pass
        # 4. Inline "Thank you for applying" text?
        if _has_inline_confirmation_text(page):
            return "confirmed"
        if time.monotonic() >= deadline:
            return "no_change"
        time.sleep(0.5)


def _has_inline_confirmation_text(page: Page) -> bool:
    """Return True iff the page (or its body) shows a Greenhouse
    inline confirmation message. Greenhouse's ``/embed/job_app`` flow
    swaps the form for a "Thank you for applying" panel without
    changing the URL."""
    try:
        body_text = page.inner_text("body", timeout=1_000)
    except PlaywrightError:
        return False
    if not body_text:
        return False
    return bool(_CONFIRMATION_TEXT_REGEX.search(body_text))


def _handle_security_code_prompt(
    page: Page,
    *,
    sent_after: _dt.datetime,
    result: AutobidResult,
    action_timeout_ms: int,
) -> bool:
    """Fetch the verification code from Gmail and paste it into the form."""
    from .email_verify import (
        VerifyEmailDisabled,
        fetch_greenhouse_security_code,
        load_gmail_config,
    )

    try:
        cfg = load_gmail_config()
    except VerifyEmailDisabled as exc:
        result.submission_status = "needs_code_failed"
        result.submission_errors.append(str(exc))
        result.error = str(exc)
        logger.error("%s", exc)
        return False

    timeout_seconds = _email_verify_timeout_seconds()
    logger.info(
        "Polling %s for the Greenhouse verification code (timeout %.0fs)...",
        cfg.address,
        timeout_seconds,
    )
    code = fetch_greenhouse_security_code(
        sent_after=sent_after,
        timeout_seconds=timeout_seconds,
        config=cfg,
    )
    if not code:
        msg = (
            "Did not receive a Greenhouse verification email within "
            f"{timeout_seconds:.0f}s. Check your inbox manually."
        )
        result.submission_status = "needs_code_failed"
        result.submission_errors.append(msg)
        result.error = msg
        logger.error(msg)
        return False

    try:
        _fill_security_code(page, code, action_timeout_ms=action_timeout_ms)
    except Exception as exc:
        msg = f"Could not paste verification code into the form: {exc}"
        result.submission_status = "needs_code_failed"
        result.submission_errors.append(msg)
        result.error = msg
        logger.error(msg)
        return False
    logger.info("Pasted verification code into the security-code field.")
    return True


def _fill_security_code(
    page: Page, code: str, *, action_timeout_ms: int
) -> None:
    """Type ``code`` into the verification-code field, regardless of
    whether the form uses a single text input or N single-character
    inputs (``security-input-0`` ... ``security-input-N-1``).

    Raises on the standard Playwright errors so the caller can fold
    them into ``submission_errors``.
    """

    per_char = page.locator(_PER_CHAR_CODE_SELECTOR)
    try:
        per_char_count = per_char.count()
    except PlaywrightError:
        per_char_count = 0

    if per_char_count >= 2:
        # Order inputs by the numeric suffix on their id so we don't
        # rely on DOM order matching visual order.
        ids: list[str] = []
        for i in range(per_char_count):
            try:
                ids.append((per_char.nth(i).get_attribute("id") or ""))
            except PlaywrightError:
                ids.append("")

        def _suffix(s: str) -> int:
            m = re.search(r"(\d+)$", s)
            return int(m.group(1)) if m else 0

        order = sorted(range(per_char_count), key=lambda i: _suffix(ids[i]))
        if len(code) != len(order):
            logger.warning(
                "Security code length mismatch: code has %d chars but "
                "form expects %d. Filling as much as fits.",
                len(code),
                len(order),
            )

        # Type each character into its OWN input. We can't rely on
        # auto-advance: the per-character inputs use ``maxlength="1"``
        # so any second character typed into the same input is just
        # rejected by the browser before React can react. Click each
        # input to focus it, then dispatch ONE keystroke. The brief
        # sleeps let React's onChange handler commit the value and
        # release focus before the next click.
        scrolled_first = False
        for i, ch in enumerate(code):
            if i >= len(order):
                break
            el = per_char.nth(order[i])
            if not scrolled_first:
                try:
                    el.scroll_into_view_if_needed(timeout=1_000)
                except PlaywrightError:
                    pass
                scrolled_first = True
            try:
                el.click(timeout=action_timeout_ms)
            except PlaywrightError:
                try:
                    el.focus()
                except PlaywrightError:
                    pass
            time.sleep(0.05)
            # ``keyboard.type`` emits real ``keydown``/``keypress``/
            # ``input``/``keyup`` events on the focused element --
            # this is what React-controlled OTP components listen to.
            page.keyboard.type(ch, delay=30)
            time.sleep(0.05)

        # Verify and log what actually landed. If any slot is wrong,
        # fall back to the React-compatible "native setter +
        # dispatchEvent('input')" trick, which bypasses any
        # ``maxlength`` / ``readonly`` / ``onKeyDown`` filtering React
        # might be doing.
        actual: list[str] = []
        for i in range(min(len(code), len(order))):
            try:
                actual.append(
                    per_char.nth(order[i]).input_value(timeout=500) or ""
                )
            except PlaywrightError:
                actual.append("")
        joined = "".join(actual)
        if joined != code:
            logger.warning(
                "After typing, code inputs read %r but expected %r; "
                "retrying via React-native value setter.",
                joined,
                code,
            )
            input_ids = [
                ids[order[i]] for i in range(min(len(code), len(order)))
            ]
            try:
                page.evaluate(
                    """(args) => {
                        const { ids, code } = args;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        for (let i = 0; i < ids.length && i < code.length; i++) {
                            const el = document.getElementById(ids[i]);
                            if (!el) continue;
                            setter.call(el, code[i]);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        // Blur the last input so any onBlur validation runs.
                        const last = document.getElementById(ids[ids.length - 1]);
                        if (last && last.blur) last.blur();
                    }""",
                    {"ids": input_ids, "code": code},
                )
            except PlaywrightError as exc:
                logger.warning("React-native value setter failed: %s", exc)
            # Re-verify.
            after: list[str] = []
            for i in range(min(len(code), len(order))):
                try:
                    after.append(
                        per_char.nth(order[i]).input_value(timeout=500) or ""
                    )
                except PlaywrightError:
                    after.append("")
            after_joined = "".join(after)
            if after_joined != code:
                logger.warning(
                    "Code inputs still mismatch after JS fallback: "
                    "got %r, expected %r.",
                    after_joined,
                    code,
                )
            else:
                logger.info("Code inputs filled via JS fallback: %r", after_joined)
        else:
            logger.info("Code inputs filled via keyboard typing: %r", joined)
        return

    # Single-input layout (``id="security_code"`` etc.).
    code_field = page.locator(_SECURITY_CODE_SELECTOR).first
    code_field.wait_for(state="visible", timeout=action_timeout_ms)
    code_field.scroll_into_view_if_needed(timeout=2_000)
    code_field.fill(code, timeout=action_timeout_ms)


def _email_verify_timeout_seconds() -> float:
    raw = (os.getenv("EMAIL_VERIFY_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 90.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 90.0


def _collect_form_errors(page: Page) -> list[str]:
    """Return a deduped list of visible error-message texts on the form."""
    errors: list[str] = []
    seen: set[str] = set()
    try:
        loc = page.locator(_FORM_ERROR_SELECTOR)
        count = loc.count()
    except PlaywrightError:
        return errors
    for i in range(min(count, 20)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() or "").strip()
        except PlaywrightError:
            continue
        if not text:
            continue
        # Trim ridiculously long blocks and dedupe.
        if len(text) > 200:
            text = text[:200] + "..."
        if text in seen:
            continue
        seen.add(text)
        errors.append(text)
    return errors


# ---------------------------------------------------------------------------
# Label / question text discovery
# ---------------------------------------------------------------------------


def _get_label(page: Page, element: Locator) -> str:
    """Compute a human-readable label for ``element``.

    Tries (in order):
      1. ``aria-label`` attribute.
      2. ``aria-labelledby`` -> referenced element's text.
      3. ``<label for=elementId>`` text.
      4. Closest ancestor ``<label>`` text.
      5. Element id as a fallback.
    """

    try:
        aria_label = (element.get_attribute("aria-label") or "").strip()
    except PlaywrightError:
        aria_label = ""
    if aria_label:
        return aria_label

    try:
        labelledby = (element.get_attribute("aria-labelledby") or "").strip()
    except PlaywrightError:
        labelledby = ""
    if labelledby:
        first_id = labelledby.split()[0]
        try:
            ref = page.locator(_id_selector(first_id)).first
            if ref.count() > 0:
                txt = (ref.inner_text(timeout=500) or "").strip()
                if txt:
                    return _trim_required_marker(txt)
        except PlaywrightError:
            pass

    try:
        el_id = (element.get_attribute("id") or "").strip()
    except PlaywrightError:
        el_id = ""
    if el_id:
        try:
            for_label = page.locator(
                f'label[for="{_css_escape(el_id)}"]'
            ).first
            if for_label.count() > 0:
                txt = (for_label.inner_text(timeout=500) or "").strip()
                if txt:
                    return _trim_required_marker(txt)
        except PlaywrightError:
            pass

    try:
        ancestor_label = element.locator("xpath=ancestor::label[1]").first
        if ancestor_label.count() > 0:
            txt = (ancestor_label.inner_text(timeout=500) or "").strip()
            if txt:
                return _trim_required_marker(txt)
    except PlaywrightError:
        pass

    return el_id


def _question_text_for_label(page: Page, element: Locator, label: str) -> str:
    """Return a richer question prompt for AI -- includes the surrounding
    question container's text if we can find one (Greenhouse wraps each
    question in a ``.application-question`` style block)."""

    try:
        container = element.locator(
            'xpath=ancestor::*[contains(@class,"application--field")'
            ' or contains(@class,"application-question")'
            ' or self::fieldset][1]'
        )
        if container.count() > 0:
            txt = (container.first.inner_text(timeout=500) or "").strip()
            if txt:
                return _trim_required_marker(txt)
    except PlaywrightError:
        pass
    return label


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_fill(element: Locator, value: str) -> bool:
    try:
        element.scroll_into_view_if_needed(timeout=1_500)
    except PlaywrightError:
        pass
    try:
        element.fill(value, timeout=4_000)
        return True
    except PlaywrightError as exc:
        logger.warning("fill() failed: %s; trying click+type fallback.", exc)
    try:
        element.click(timeout=2_000)
        element.press_sequentially(value, delay=15)
        return True
    except PlaywrightError as exc:
        logger.warning("click+type fallback failed: %s", exc)
        return False


def _trim_required_marker(text: str) -> str:
    """Strip trailing required-asterisk / parenthetical markers from labels."""

    text = text.replace("\u00a0", " ").strip()
    # Common patterns: "First Name *", "First Name (required)", "First Name ✱"
    text = re.sub(r"[\s]*\*+\s*$", "", text)
    text = re.sub(
        r"\s*\((required|optional|en français)\)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Many forms render label + helper text with newlines; keep just the
    # first non-empty line for AI label / mapping purposes.
    first_line = next(
        (ln.strip() for ln in text.splitlines() if ln.strip()),
        text,
    )
    return first_line


def _css_escape(value: str) -> str:
    """Escape a string so it can be used inside a CSS attribute selector."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _id_selector(value: str) -> str:
    """Return a CSS selector matching the element whose id is ``value``,
    safe for IDs that contain characters CSS doesn't accept after ``#``
    (most importantly leading digits, e.g. ``"4009866005"``). We use the
    attribute-selector form ``[id="..."]`` because it works for any
    valid HTML id without needing to spell-out CSS-escape rules."""

    if not value:
        return ""
    return f'[id="{_css_escape(value)}"]'


def is_supported_url(url: str) -> bool:
    """True if the autobid module can handle the given external URL."""

    return "job-boards.greenhouse.io" in (url or "")
