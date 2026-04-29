"""Applicant profile loaded from .env, used by the autobid module.

The fields here mirror what most Greenhouse application forms ask for. Each
field is optional (an empty string skips filling that field), so users can
leave anything they don't want to autofill blank.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Cap the resume text we feed to the AI so prompts stay within model
# context limits even for unusually long CVs. ~12k chars is a safe cap
# for current generation models (gpt-4o-mini and friends) while still
# leaving room for the question + options + system instructions.
_RESUME_TEXT_MAX_CHARS = 12_000


@dataclass(frozen=True)
class ApplicantProfile:
    # Basics
    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    country: str = ""

    # Online presence
    linkedin: str = ""
    website: str = ""
    github: str = ""

    # Address
    city: str = ""
    state: str = ""
    # Two-letter postal abbreviation (e.g. "TX"). Combined with ``city``
    # for any form field that asks for "City, State" -- see
    # ``city_state`` below.
    state_abbr: str = ""
    postal_code: str = ""

    # Used for typeahead "Location (City)" comboboxes that don't share the
    # candidate's home city. Falls back to ``city`` when empty.
    location_city: str = ""

    # Education -- used when a Greenhouse form has a typeahead
    # School / Degree / Discipline section plus Start/End year selects.
    school: str = ""
    degree: str = ""
    discipline: str = ""
    education_start_year: str = ""
    education_end_year: str = ""
    # Some forms split the date into separate month + year selects.
    # Optional; falls back to the AI when blank.
    education_start_month: str = ""
    education_end_month: str = ""

    # Resume file (absolute path or relative to project root) and its
    # extracted plain-text content, used to ground AI-generated answers
    # in the candidate's actual experience.
    resume_path: Path | None = None
    resume_text: str = ""
    # OPTIONAL: folder of candidate resumes used by the per-job
    # judge/match pipeline. When set, the autobid scores every PDF
    # against the JD and uploads the best match instead of
    # ``resume_path``. ``resume_path`` is still used as the fallback
    # if the matcher fails / is disabled.
    resumes_dir: Path | None = None

    # Voluntary self-identification (Greenhouse dropdown labels)
    gender: str = ""
    hispanic_ethnicity: str = ""
    race: str = ""
    veteran_status: str = ""
    disability_status: str = ""

    # Optional "U.S. Standard Demographic" survey fields (some employers
    # render these on top of the EEO ones). Multi-value entries are
    # comma-separated strings -- the autobid splits them when filling
    # multi-select dropdowns. Leave blank to let the AI pick.
    gender_identity: str = ""
    racial_ethnic_background: str = ""
    sexual_orientation: str = ""
    transgender: str = ""
    # Short Yes/No answers for the matching demographic single-select
    # questions, e.g. "Do you have a disability or chronic condition...?"
    # and "Are you a veteran or active member of the U.S. Armed Forces?".
    disability_chronic: str = ""
    veteran_active: str = ""

    # Other common application-form questions that aren't covered by
    # any standard Greenhouse field. Set these to keep the AI from
    # being asked at all.
    referral_source: str = ""
    security_clearance: str = ""

    # Free-text candidate summary used to ground AI-generated answers.
    summary: str = ""

    # Friendly aliases used by the autobid form-filler when matching by
    # input id / aria-label. Lower-case keys.
    text_field_aliases: dict[str, str] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def city_state(self) -> str:
        """``"<city>, <state_abbr>"`` if both are set; otherwise just
        the city (or just the state if the city is missing). Used for
        form fields that ask for "City, State" -- e.g. afresh's
        "In which U.S. city & state do you currently reside?"."""

        city = (self.city or "").strip()
        abbr = (self.state_abbr or "").strip()
        # Fall back to the full state name if no abbreviation was set.
        if not abbr:
            abbr = (self.state or "").strip()
        if city and abbr:
            return f"{city}, {abbr}"
        return city or abbr


def load_profile(project_root: Path | None = None) -> ApplicantProfile:
    """Load the applicant profile from the current process environment."""

    root = (project_root or Path.cwd()).resolve()

    def env(name: str, default: str = "") -> str:
        value = os.getenv(name)
        return value.strip() if value else default

    resume_raw = env("APPLICANT_RESUME_PATH")
    if resume_raw:
        resume_path: Path | None = Path(resume_raw)
        if not resume_path.is_absolute():
            resume_path = (root / resume_path).resolve()
    else:
        resume_path = None

    resumes_dir_raw = env("APPLICANT_RESUMES_DIR")
    if resumes_dir_raw:
        resumes_dir: Path | None = Path(resumes_dir_raw)
        if not resumes_dir.is_absolute():
            resumes_dir = (root / resumes_dir).resolve()
        if not resumes_dir.exists():
            logger.warning(
                "APPLICANT_RESUMES_DIR=%s does not exist; per-job resume "
                "matching disabled. The single APPLICANT_RESUME_PATH will "
                "be used for every job.",
                resumes_dir,
            )
            resumes_dir = None
    else:
        resumes_dir = None

    resume_text = ""
    if resume_path and resume_path.exists():
        resume_text = _extract_resume_text(resume_path)
        if resume_text:
            logger.info(
                "Loaded %d chars of resume text from %s for AI grounding.",
                len(resume_text),
                resume_path.name,
            )

    profile = ApplicantProfile(
        first_name=env("APPLICANT_FIRST_NAME"),
        last_name=env("APPLICANT_LAST_NAME"),
        preferred_name=env("APPLICANT_PREFERRED_NAME"),
        email=env("APPLICANT_EMAIL"),
        phone=env("APPLICANT_PHONE"),
        country=env("APPLICANT_COUNTRY"),
        linkedin=env("APPLICANT_LINKEDIN"),
        website=env("APPLICANT_WEBSITE"),
        github=env("APPLICANT_GITHUB"),
        city=env("APPLICANT_CITY"),
        state=env("APPLICANT_STATE"),
        state_abbr=env("APPLICANT_STATE_ABBR"),
        postal_code=env("APPLICANT_POSTAL_CODE"),
        location_city=env("APPLICANT_LOCATION_CITY") or env("APPLICANT_CITY"),
        school=env("APPLICANT_SCHOOL"),
        degree=env("APPLICANT_DEGREE"),
        discipline=env("APPLICANT_DISCIPLINE"),
        education_start_year=env("APPLICANT_EDUCATION_START_YEAR"),
        education_end_year=env("APPLICANT_EDUCATION_END_YEAR"),
        education_start_month=env("APPLICANT_EDUCATION_START_MONTH"),
        education_end_month=env("APPLICANT_EDUCATION_END_MONTH"),
        resume_path=resume_path,
        resume_text=resume_text,
        resumes_dir=resumes_dir,
        gender=env("APPLICANT_GENDER"),
        hispanic_ethnicity=env("APPLICANT_HISPANIC_ETHNICITY"),
        race=env("APPLICANT_RACE"),
        veteran_status=env("APPLICANT_VETERAN_STATUS"),
        disability_status=env("APPLICANT_DISABILITY_STATUS"),
        gender_identity=env("APPLICANT_GENDER_IDENTITY"),
        racial_ethnic_background=env("APPLICANT_RACIAL_ETHNIC_BACKGROUND"),
        sexual_orientation=env("APPLICANT_SEXUAL_ORIENTATION"),
        transgender=env("APPLICANT_TRANSGENDER"),
        disability_chronic=env("APPLICANT_DISABILITY_CHRONIC"),
        veteran_active=env("APPLICANT_VETERAN_ACTIVE"),
        referral_source=env("APPLICANT_REFERRAL_SOURCE"),
        security_clearance=env("APPLICANT_SECURITY_CLEARANCE"),
        summary=env("APPLICANT_SUMMARY"),
    )
    return profile


def _extract_resume_text(path: Path) -> str:
    """Extract plain text from a resume file for use as AI grounding.

    Supports PDF (via pypdf) and plain-text resumes. For other formats
    (.docx, .rtf, ...) we just return an empty string and let the AI
    rely on the candidate summary; a warning is logged so the user
    knows the resume isn't being read.
    """

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _extract_pdf_text(path)
        if suffix in (".txt", ".md"):
            return _truncate_resume(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        logger.warning("Could not read resume %s: %s", path, exc)
        return ""

    logger.warning(
        "Resume %s has unsupported extension %r for text extraction; "
        "AI answers will fall back to APPLICANT_SUMMARY only.",
        path.name,
        suffix or "(none)",
    )
    return ""


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning(
            "pypdf is not installed; resume PDF will not be parsed. "
            "Run `pip install pypdf` to enable AI grounding on resume content."
        )
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pypdf raises various Exception subclasses
        logger.warning("Failed to open resume PDF %s: %s", path, exc)
        return ""

    chunks: list[str] = []
    for page_num, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - rare malformed page
            logger.debug("Skipped resume page %d (extract failed: %s)", page_num, exc)
            continue
        text = text.strip()
        if text:
            chunks.append(text)

    return _truncate_resume("\n\n".join(chunks))


def _truncate_resume(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= _RESUME_TEXT_MAX_CHARS:
        return cleaned
    return cleaned[: _RESUME_TEXT_MAX_CHARS].rstrip() + "\n\n[...resume truncated for prompt size...]"
