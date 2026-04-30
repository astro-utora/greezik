"""
Step 0 — Judge whether Job_Description.txt is worth bidding.

Reads Job_Description.txt and decides BID or SKIP using three layers:

  Layer A  Role-type BLACKLIST  (hard reject)
           Consultant / Security / Manager / Analyst / Admin / DBA-only / etc.
           These roles are not what we bid on even if they mention "software".

  Layer B  Hard DISQUALIFIERS
           Active security clearance, US-citizen-only with clearance,
           non-software domain (mechanical/hardware/firmware-only),
           pure-functional (SAP FICO, Salesforce Admin, Workday Functional),
           intern / entry-level, etc.

  Layer C  FIT SCORE against resume vocabulary
           Build a term frequency vocabulary from _resume_cache.json
           (every resume is considered a valid software-engineering bid target).
           Count how many of those terms appear in the JD and weight the
           ones that match our TECH phrases (imported from step1) higher.
           Also require at least one "software-engineer intent" verb/noun
           (develop, build, code, api, software, application, ...).

VERDICT:
  BID   — passes all three layers.
  SKIP  — fails any layer. Reasons are printed.

Exit code is 0 for BID, 1 for SKIP, so you can chain it in a shell:
    python step0_judge_jd.py && python step1_match_resume.py && ...

Tune the lists below to taste — they are the only knobs you need.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# Reuse the curated tech vocabulary from step1 so judgement and matching
# stay consistent.
from step1_match_resume import TECH_PHRASES, STOP_WORDS, _word_boundary_check

BASE_DIR = Path(__file__).resolve().parent.parent
STEPS_DIR = Path(__file__).resolve().parent
JOB_DESC_PATH = BASE_DIR / "bid" / "Job_Description.txt"
CACHE_PATH = STEPS_DIR / "_resume_cache.json"
# Persistent cache of the tech-phrase frequency vocabulary so we don't
# re-scan every resume in _resume_cache.json on every run. Keyed on the
# resume cache's (size, mtime) so it auto-refreshes when resumes change.
VOCAB_CACHE_PATH = STEPS_DIR / "_judge_vocab_cache.json"


# ─────────────────────────────────────────────────────────────
# Layer A — Role-type BLACKLIST (whole-phrase match, case-insensitive)
# Applied against the JOB TITLE (first non-empty line).
# ─────────────────────────────────────────────────────────────
TITLE_BLACKLIST = [
    # Non-engineering roles
    # NOTE: "consultant" / "consulting" are NOT in this hard list because
    # postings often prefix with "W2 Consultant" (= contractor status) while
    # the actual role is Developer / Engineer. They are handled in
    # SOFT_TITLE_BLACKLIST below, which only fires when no dev keyword is
    # also present in the title.
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
    # Security-only
    "security engineer", "security analyst", "security architect",
    "application security", "appsec", "information security",
    "soc analyst", "penetration tester", "pentest", "red team",
    "cybersecurity", "cyber security", "security operations",
    "iam engineer", "grc analyst",
    # Pure ops / admin / support
    "system administrator", "sysadmin", "network engineer",
    "network administrator", "network architect",
    "database administrator",
    # NOTE: "dba" alone is removed — "Oracle DBA" / "PL/SQL DBA" type roles
    # are scripting-heavy and are valid bid targets.
    "help desk", "helpdesk", "service desk",
    "desktop support", "technical support",
    "production support", "application support",
    # NOTE: "site reliability" / "sre" removed — SRE / DevOps-SRE roles are
    # software-engineering work (Python/Go tooling, IaC, automation) and
    # are valid bid targets.
    # QA-only (user has SDET resumes, but pure manual QA is skip)
    "manual qa", "manual tester", "qa tester", "test analyst",
    "test manager", "qa manager", "qa lead",
    # Data roles that are analysis-only
    "data analyst", "business intelligence analyst", "bi analyst",
    "reporting analyst", "tableau developer only",
    # ERP / functional consultants (not dev)
    "sap functional", "sap basis", "sap fico", "sap mm", "sap sd",
    "sap hr", "sap abap", "sap consultant",
    "salesforce admin", "salesforce administrator",
    "workday functional", "workday consultant", "workday analyst",
    "servicenow admin", "servicenow administrator",
    "oracle functional", "peoplesoft functional",
    # Non-software engineering
    "mechanical engineer", "electrical engineer", "hardware engineer",
    "firmware engineer", "fpga engineer", "asic engineer",
    "embedded systems engineer",  # careful — often needs C/C++; treat as skip by default
    "rf engineer", "chemical engineer", "civil engineer",
    "validation engineer", "test engineer - hardware",
    # Game / graphics very specialized
    "game designer", "level designer",
    # Entry-level
    "intern", "internship", "co-op", "junior associate",
    "entry level", "entry-level", "new grad", "graduate program",
    # Executive
    "cto", "cio", "ciso", "vp of engineering", "director of engineering",
    "head of engineering", "head of product", "chief architect",
]

# ─────────────────────────────────────────────────────────────
# Layer A2 — SOFT title blacklist. These terms only cause a SKIP when
# the title contains NO positive dev keyword (Developer / Engineer /
# Architect / Programmer / SDE / SWE). Many real Dice postings carry
# words like "Consultant" as a contracting label rather than the role
# (e.g. "W2 Consultant || Lead Full Stack Java Developer").
# ─────────────────────────────────────────────────────────────
SOFT_TITLE_BLACKLIST = [
    "consultant", "consulting",
]

# Positive overrides — if any of these appear in the title we KEEP the
# job even when a SOFT_TITLE_BLACKLIST term also appears.
TITLE_DEV_OVERRIDE = [
    "developer", "developers",
    "engineer", "engineers",
    "programmer", "programmers",
    "architect",
    "sdet", "sde", "swe",
    "full stack", "full-stack", "fullstack",
    "backend", "back-end", "back end",
    "frontend", "front-end", "front end",
    "software",
]

# ─────────────────────────────────────────────────────────────
# Layer B — Hard DISQUALIFIERS (any single hit = SKIP)
# ─────────────────────────────────────────────────────────────
HARD_DISQUALIFIERS = [
    # Clearance-gated
    r"\b(active\s+)?(ts/sci|top\s+secret|secret\s+clearance)\b",
    r"\bactive\s+(security\s+)?clearance\b",
    r"\bpolygraph\s+required\b",
    # Pure hardware / non-software
    r"\bpcb\s+design\b",
    r"\bverilog\b", r"\bvhdl\b",
    r"\banalog\s+circuit\b",
    # Purely functional ERP
    r"\bfunctional\s+consultant\b",
    r"\bbusiness\s+process\s+consultant\b",
    # Manual testing only (explicit "no automation")
    r"\bmanual\s+testing\s+only\b",
    # Internship / new grad explicit
    r"\b(internship|intern\s+position|co-op\s+position)\b",
    r"\bnew\s+grad(uate)?\s+program\b",
]

# ─────────────────────────────────────────────────────────────
# Layer C — SWE intent keywords. At least one must appear.
# ─────────────────────────────────────────────────────────────
SWE_INTENT = [
    "software", "developer", "developers", "engineer", "engineers",
    "programming", "programmer", "programmers",
    "code", "coding", "develop", "develops", "developing", "development",
    "api", "apis", "rest", "microservice", "microservices",
    "backend", "back-end", "back end", "frontend", "front-end", "front end",
    "full stack", "full-stack", "fullstack",
    "application", "applications", "platform", "platforms",
    "build", "builds", "building",
    "design and implement", "implement", "implements", "implementing",
    "scripting", "automation", "sdk", "sdks",
    # Database / data-platform engineering roles also involve scripting,
    # tuning, ETL, and pipeline work \u2014 treat as SWE intent.
    "sql", "pl/sql", "t-sql", "stored procedure", "stored procedures",
    "dba", "etl", "elt", "data pipeline", "data pipelines",
]

# Minimum fit score (sum of matched tech-phrase weights) required to BID
# for a NORMAL-length JD. Tune after running against a few sample JDs.
# Lowered from 4.0 → 2.5 after auditing skipped jobs (.NET Developer,
# Databricks Data Engineer, Quadient Inspire, OIC Developer, Datacap
# Developer, etc. were valid bid targets being rejected on score alone).
MIN_FIT_SCORE = 2.5
# Minimum number of DISTINCT tech phrases matched (avoid single-keyword fluke).
# Lowered from 2 → 1 — many specialized-stack postings name the platform
# (e.g. "Quadient Inspire", "Datacap") only once or twice; combined with the
# title-blacklist + hard-disqualifier checks, 1 distinct tech phrase is
# enough signal that the role is engineering work.
MIN_DISTINCT_TECH_HITS = 1

# A JD with fewer than this many words is treated as "short" (e.g. a posting
# that is essentially just a title like "Sr. Automation Engineer (Java/Selenium)").
# For short JDs we can't expect a large fit score, so we use relaxed thresholds:
# 1 tech phrase + any positive fit + SWE intent is enough, as long as no
# blacklist / disqualifier rule fires.
SHORT_JD_WORD_THRESHOLD = 30
SHORT_JD_MIN_FIT_SCORE = 0.5
SHORT_JD_MIN_DISTINCT_TECH_HITS = 1


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _read_jd() -> str:
    return JOB_DESC_PATH.read_text(encoding="utf-8", errors="ignore")


def _extract_title(jd: str) -> str:
    for line in jd.splitlines():
        line = line.strip()
        if line and len(line) < 200:
            return line
    return ""


def _build_resume_vocab() -> dict[str, int]:
    """Aggregate tech-phrase frequencies across every cached resume.
    Resume-frequency acts as a prior: phrases common in our resumes are
    phrases we can legitimately claim, so matching them in a JD is
    strong positive signal.

    The result is memoized on disk at VOCAB_CACHE_PATH, keyed on the
    (size, mtime) of _resume_cache.json. On subsequent runs we just
    load the small JSON instead of re-scanning ~240 resumes. The cache
    is also invalidated automatically if TECH_PHRASES changes.
    """
    if not CACHE_PATH.exists():
        return {}

    st = CACHE_PATH.stat()
    # A stable fingerprint of the current TECH_PHRASES list; if the
    # vocabulary changes in step1, rebuild. (Don't use hash() — it is
    # salted per-process on CPython and would invalidate the cache
    # on every run.)
    phrases_digest = hashlib.sha1(
        "\n".join(TECH_PHRASES).encode("utf-8")
    ).hexdigest()[:16]
    phrases_sig = f"{len(TECH_PHRASES)}:{phrases_digest}"
    key = {
        "resume_cache_size": st.st_size,
        "resume_cache_mtime": int(st.st_mtime),
        "tech_phrases_sig": phrases_sig,
    }

    if VOCAB_CACHE_PATH.exists():
        try:
            cached = json.loads(VOCAB_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("key") == key and isinstance(cached.get("vocab"), dict):
                return {k: int(v) for k, v in cached["vocab"].items()}
        except Exception:
            pass  # fall through and rebuild

    vocab: dict[str, int] = {}
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return vocab
    for entry in cache.values():
        text = (entry or {}).get("text", "")
        if not text:
            continue
        low = text.lower()
        for ph in TECH_PHRASES:
            if _word_boundary_check(low, ph):
                vocab[ph] = vocab.get(ph, 0) + 1

    try:
        VOCAB_CACHE_PATH.write_text(
            json.dumps({"key": key, "vocab": vocab}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # caching is best-effort

    return vocab


def _match_tech_phrases(jd_lower: str) -> list[str]:
    return [ph for ph in TECH_PHRASES if _word_boundary_check(jd_lower, ph)]


def _has_any(text_lower: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if _word_boundary_check(text_lower, p)]


def _regex_hits(text_lower: str, patterns: list[str]) -> list[str]:
    hits = []
    for p in patterns:
        if re.search(p, text_lower, re.IGNORECASE):
            hits.append(p)
    return hits


# ─────────────────────────────────────────────────────────────
# Main judge
# ─────────────────────────────────────────────────────────────
def judge(jd_text: str) -> tuple[bool, list[str], dict]:
    """Return (should_bid, reasons, details)."""
    reasons: list[str] = []
    details: dict = {}

    title = _extract_title(jd_text)
    title_lower = title.lower()
    body_lower = jd_text.lower()

    # Detect "short" JDs (essentially just a title). For these we relax the
    # fit-score / distinct-phrase thresholds — the title alone is the signal.
    word_count = len(re.findall(r"\w+", jd_text))
    is_short = word_count < SHORT_JD_WORD_THRESHOLD
    details["word_count"] = word_count
    details["is_short_jd"] = is_short
    min_fit = SHORT_JD_MIN_FIT_SCORE if is_short else MIN_FIT_SCORE
    min_hits = SHORT_JD_MIN_DISTINCT_TECH_HITS if is_short else MIN_DISTINCT_TECH_HITS

    # Layer A1 — title blacklist (hard reject)
    title_hits = _has_any(title_lower, TITLE_BLACKLIST)
    details["title"] = title
    details["title_blacklist_hits"] = title_hits
    if title_hits:
        reasons.append(f"Title matches blacklist: {title_hits}")

    # Layer A2 — soft title blacklist (skip only if no dev keyword present)
    soft_hits = _has_any(title_lower, SOFT_TITLE_BLACKLIST)
    dev_overrides = _has_any(title_lower, TITLE_DEV_OVERRIDE)
    details["soft_title_blacklist_hits"] = soft_hits
    details["title_dev_overrides"] = dev_overrides
    if soft_hits and not dev_overrides:
        reasons.append(
            f"Title soft-blacklist hit with no dev override: {soft_hits}"
        )

    # Layer B — hard disqualifiers
    dq = _regex_hits(body_lower, HARD_DISQUALIFIERS)
    details["hard_disqualifier_hits"] = dq
    if dq:
        reasons.append(f"Hard disqualifier: {dq}")

    # Layer C — SWE intent
    intent_hits = _has_any(body_lower, SWE_INTENT)
    details["swe_intent_hits"] = intent_hits
    if not intent_hits:
        reasons.append("No software-engineering intent keywords found.")

    # Layer C — tech phrase overlap and weighted fit score
    jd_phrases = _match_tech_phrases(body_lower)
    resume_vocab = _build_resume_vocab()
    # Weight each matched phrase by how many of our resumes contain it
    # (normalized by max), so phrases we can genuinely claim are worth more.
    max_rf = max(resume_vocab.values()) if resume_vocab else 1
    fit_score = 0.0
    per_phrase = {}
    for ph in jd_phrases:
        rf = resume_vocab.get(ph, 0)
        # Always give at least 0.5 for an in-vocabulary tech phrase;
        # scale up to ~2.0 for phrases common in our resumes.
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
        reasons.append(
            f"Fit score {fit_score:.2f} below threshold {min_fit}."
        )

    # Short-JD override: if the posting is essentially just a title and it
    # passes all the "must-not" filters (not blacklisted, not disqualified,
    # has SWE intent), accept it even with zero tech-phrase hits. A clean
    # title like "Android Developer" or "Senior Engineer" deserves the
    # benefit of the doubt when there is literally no body text to score.
    if is_short and not title_hits and not dq and intent_hits:
        reasons = [
            r for r in reasons
            if not r.startswith("Too few distinct tech phrases")
            and not r.startswith("Fit score")
        ]
        details["short_jd_override"] = True

    should_bid = len(reasons) == 0
    return should_bid, reasons, details


def main() -> int:
    if not JOB_DESC_PATH.exists():
        print(f"ERROR: {JOB_DESC_PATH} not found.")
        return 2

    jd = _read_jd()
    should_bid, reasons, details = judge(jd)

    print("=" * 60)
    print(f"Title: {details.get('title')!r}")
    print(f"Word count: {details['word_count']}"
          + ("  (short JD — relaxed thresholds)" if details["is_short_jd"] else ""))
    print("-" * 60)
    print(f"Distinct tech phrases matched: {details['distinct_tech_hits']}")
    threshold_fit = SHORT_JD_MIN_FIT_SCORE if details["is_short_jd"] else MIN_FIT_SCORE
    print(f"Fit score: {details['fit_score']} (threshold {threshold_fit})")
    if details["matched_tech_phrases"]:
        top = sorted(
            details["per_phrase_weight"].items(),
            key=lambda kv: kv[1],
            reverse=True,
        )[:15]
        print(f"Top matched phrases: {top}")
    print(f"SWE intent keywords: {details['swe_intent_hits'][:8]}")
    if details["title_blacklist_hits"]:
        print(f"Title blacklist hits: {details['title_blacklist_hits']}")
    if details["hard_disqualifier_hits"]:
        print(f"Hard disqualifier hits: {details['hard_disqualifier_hits']}")
    print("-" * 60)

    if should_bid:
        print("VERDICT: BID")
        return 0
    else:
        print("VERDICT: SKIP")
        for r in reasons:
            print(f"  - {r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
