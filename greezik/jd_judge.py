"""Judge whether a JD is worth bidding on.

Ported from ``dice_auto_bidder/steps/step0_judge_jd.py`` and adapted to
take a JD string + :class:`ResumeIndex` directly (no file I/O at this
layer). The three layers are unchanged:

  Layer A  Title BLACKLIST   (consultant / manager / security / DBA / ...)
  Layer B  Hard DISQUALIFIERS (active clearance, hardware-only, intern, ...)
  Layer C  FIT SCORE         (tech-phrase overlap weighted by resume freq)

Public entrypoint: :func:`judge_jd` returns ``(should_bid, reasons, details)``.
"""

from __future__ import annotations

import re

from .resume_index import ResumeIndex
from .resume_match import (
    STOP_WORDS,  # noqa: F401  -- re-exported for parity with the original
    TECH_PHRASES,
    _word_boundary_check,
)


# ---------------------------------------------------------------------------
# Layer A -- Title blacklist
# ---------------------------------------------------------------------------
TITLE_BLACKLIST: list[str] = [
    "consultant", "consulting",
    "business analyst", "business system analyst", "bsa",
    "product manager", "product owner", "project manager",
    "program manager", "delivery manager", "scrum master",
    "engineering manager", "people manager", "team lead manager",
    "technical recruiter", "recruiter", "talent acquisition",
    "sales engineer", "pre-sales", "presales", "solutions sales",
    "account executive", "customer success",
    "technical writer", "instructional designer",
    "ux designer", "ui designer", "graphic designer", "visual designer",
    "product designer", "web designer",
    "security engineer", "security analyst", "security architect",
    "application security", "appsec", "information security",
    "soc analyst", "penetration tester", "pentest", "red team",
    "cybersecurity", "cyber security", "security operations",
    "iam engineer", "grc analyst",
    "system administrator", "sysadmin", "network engineer",
    "network administrator", "network architect",
    "database administrator", "dba",
    "help desk", "helpdesk", "service desk",
    "desktop support", "technical support",
    "production support", "application support",
    "site reliability", "sre",
    "manual qa", "manual tester", "qa tester", "test analyst",
    "test manager", "qa manager", "qa lead",
    "data analyst", "business intelligence analyst", "bi analyst",
    "reporting analyst", "tableau developer only",
    "sap functional", "sap basis", "sap fico", "sap mm", "sap sd",
    "sap hr", "sap abap", "sap consultant",
    "salesforce admin", "salesforce administrator",
    "workday functional", "workday consultant", "workday analyst",
    "servicenow admin", "servicenow administrator",
    "oracle functional", "peoplesoft functional",
    "mechanical engineer", "electrical engineer", "hardware engineer",
    "firmware engineer", "fpga engineer", "asic engineer",
    "embedded systems engineer",
    "rf engineer", "chemical engineer", "civil engineer",
    "validation engineer", "test engineer - hardware",
    "game designer", "level designer",
    "intern", "internship", "co-op", "junior associate",
    "entry level", "entry-level", "new grad", "graduate program",
    "cto", "cio", "ciso", "vp of engineering", "director of engineering",
    "head of engineering", "head of product", "chief architect",
]

# ---------------------------------------------------------------------------
# Layer B -- Hard disqualifiers (regex)
# ---------------------------------------------------------------------------
HARD_DISQUALIFIERS: list[str] = [
    r"\b(active\s+)?(ts/sci|top\s+secret|secret\s+clearance)\b",
    r"\bactive\s+(security\s+)?clearance\b",
    r"\bpolygraph\s+required\b",
    r"\bpcb\s+design\b",
    r"\bverilog\b", r"\bvhdl\b",
    r"\banalog\s+circuit\b",
    r"\bfunctional\s+consultant\b",
    r"\bbusiness\s+process\s+consultant\b",
    r"\bmanual\s+testing\s+only\b",
    r"\b(internship|intern\s+position|co-op\s+position)\b",
    r"\bnew\s+grad(uate)?\s+program\b",
]

# ---------------------------------------------------------------------------
# Layer C -- SWE intent + thresholds
# ---------------------------------------------------------------------------
SWE_INTENT: list[str] = [
    "software", "developer", "engineer", "programming", "programmer",
    "code", "coding", "develop", "development",
    "api", "apis", "rest", "microservice", "microservices",
    "backend", "back-end", "back end", "frontend", "front-end", "front end",
    "full stack", "full-stack", "fullstack",
    "application", "applications", "platform",
    "build", "design and implement", "implement",
]

MIN_FIT_SCORE = 4.0
MIN_DISTINCT_TECH_HITS = 2

SHORT_JD_WORD_THRESHOLD = 30
SHORT_JD_MIN_FIT_SCORE = 0.5
SHORT_JD_MIN_DISTINCT_TECH_HITS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_title(jd: str) -> str:
    for line in jd.splitlines():
        line = line.strip()
        if line and len(line) < 200:
            return line
    return ""


def _has_any(text_lower: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if _word_boundary_check(text_lower, p)]


def _regex_hits(text_lower: str, patterns: list[str]) -> list[str]:
    hits: list[str] = []
    for p in patterns:
        if re.search(p, text_lower, re.IGNORECASE):
            hits.append(p)
    return hits


def _build_resume_vocab(index: ResumeIndex) -> dict[str, int]:
    """Aggregate tech-phrase document-frequencies across the resume corpus."""
    vocab: dict[str, int] = {}
    for entry in index.entries:
        if not entry.text:
            continue
        low = entry.text_lower
        for ph in TECH_PHRASES:
            if _word_boundary_check(low, ph):
                vocab[ph] = vocab.get(ph, 0) + 1
    return vocab


def _match_tech_phrases(jd_lower: str) -> list[str]:
    return [ph for ph in TECH_PHRASES if _word_boundary_check(jd_lower, ph)]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def judge_jd(jd_text: str, index: ResumeIndex) -> tuple[bool, list[str], dict]:
    """Return ``(should_bid, reasons, details)`` for ``jd_text``.

    ``reasons`` is empty iff ``should_bid`` is True. ``details`` exposes
    the intermediate signals (matched phrases, fit score, blacklist /
    disqualifier hits) so they can be logged for diagnosis.
    """

    reasons: list[str] = []
    details: dict = {}

    title = _extract_title(jd_text)
    title_lower = title.lower()
    body_lower = jd_text.lower()

    word_count = len(re.findall(r"\w+", jd_text))
    is_short = word_count < SHORT_JD_WORD_THRESHOLD
    details["word_count"] = word_count
    details["is_short_jd"] = is_short
    min_fit = SHORT_JD_MIN_FIT_SCORE if is_short else MIN_FIT_SCORE
    min_hits = SHORT_JD_MIN_DISTINCT_TECH_HITS if is_short else MIN_DISTINCT_TECH_HITS

    title_hits = _has_any(title_lower, TITLE_BLACKLIST)
    details["title"] = title
    details["title_blacklist_hits"] = title_hits
    if title_hits:
        reasons.append(f"Title matches blacklist: {title_hits}")

    dq = _regex_hits(body_lower, HARD_DISQUALIFIERS)
    details["hard_disqualifier_hits"] = dq
    if dq:
        reasons.append(f"Hard disqualifier: {dq}")

    intent_hits = _has_any(body_lower, SWE_INTENT)
    details["swe_intent_hits"] = intent_hits
    if not intent_hits:
        reasons.append("No software-engineering intent keywords found.")

    jd_phrases = _match_tech_phrases(body_lower)
    resume_vocab = _build_resume_vocab(index)
    max_rf = max(resume_vocab.values()) if resume_vocab else 1
    fit_score = 0.0
    per_phrase: dict[str, float] = {}
    for ph in jd_phrases:
        rf = resume_vocab.get(ph, 0)
        w = 0.5 + 1.5 * (rf / max_rf)
        fit_score += w
        per_phrase[ph] = round(w, 2)
    details["matched_tech_phrases"] = jd_phrases
    details["per_phrase_weight"] = per_phrase
    details["fit_score"] = round(fit_score, 2)
    details["distinct_tech_hits"] = len(jd_phrases)

    if len(jd_phrases) < min_hits:
        reasons.append(
            f"Too few distinct tech phrases matched "
            f"({len(jd_phrases)} < {min_hits})."
        )
    if fit_score < min_fit:
        reasons.append(f"Fit score {fit_score:.2f} below threshold {min_fit}.")

    if is_short and not title_hits and not dq and intent_hits:
        reasons = [
            r for r in reasons
            if not r.startswith("Too few distinct tech phrases")
            and not r.startswith("Fit score")
        ]
        details["short_jd_override"] = True

    should_bid = len(reasons) == 0
    return should_bid, reasons, details
