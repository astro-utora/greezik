"""AI-powered answers for free-form application questions.

The prompt template lives in a text file (default ``ai_prompt.txt``) so the
user can edit it without touching the code. Placeholders are simple
``{name}`` formats expanded against the candidate profile + question
context.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .profile import ApplicantProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing table for cost estimation
# ---------------------------------------------------------------------------

# (input $/1M tokens, output $/1M tokens) — standard tier, April 2026.
# Keys are matched by prefix (longest first) so versioned names like
# "gpt-4o-mini-2024-07-18" resolve to the right row.
_MODEL_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-5.5":       (5.00,  30.00),
    "gpt-5.4-mini":  (0.75,   4.50),
    "gpt-5.4-nano":  (0.20,   1.25),
    "gpt-5.4":       (2.50,  15.00),
    "gpt-4o-mini":   (0.15,   0.60),
    "gpt-4o":        (2.50,  10.00),
    "gpt-4-turbo":  (10.00,  30.00),
    "gpt-3.5-turbo": (0.50,   1.50),
}


def _model_price(model: str) -> tuple[float, float]:
    """Return ``(input $/1M, output $/1M)`` for *model*, matched by prefix.

    Falls back to ``(0.0, 0.0)`` for unknown models so cost is reported
    as 0 rather than raising.
    """
    lc = model.lower()
    for key in sorted(_MODEL_PRICE_PER_1M, key=len, reverse=True):
        if lc.startswith(key):
            return _MODEL_PRICE_PER_1M[key]
    return (0.0, 0.0)


# The three "kinds" of question the autobid can ask the AI. Used both
# for caching and for telling the batch endpoint how to shape its reply
# (string vs single option vs list of options).
AnswerKind = Literal["answer", "pick_option", "pick_multi"]


@dataclass(frozen=True)
class QuestionContext:
    label: str
    question: str
    # For multi-choice / dropdown questions, the available options.
    options: tuple[str, ...] = ()


@dataclass
class BatchItem:
    """One entry in a batched AI call: a question + how it should be
    answered. ``id`` is a stable string used as the JSON key for the
    response and the cache key suffix."""

    id: str
    kind: AnswerKind
    ctx: QuestionContext


class AIAnswerer:
    """Wrapper around an AI client used to answer unknown form questions.

    Constructed once (loads the prompt template + spins up the client) and
    reused across multiple questions to avoid repeated init cost.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        prompt_template: str,
        profile: ApplicantProfile,
    ) -> None:
        self.provider = provider.lower().strip()
        self.api_key = api_key
        self.model = model
        self.prompt_template = prompt_template
        # ``profile`` is exposed as a property below. The per-job resume
        # matcher swaps a fresh ApplicantProfile in mid-run (different
        # ``resume_path`` / ``resume_text``), and the setter clears the
        # answer cache so we never serve answers grounded in the previous
        # CV. Use the leading-underscore attribute here to bypass the
        # cache-clearing logic during construction.
        self._profile = profile
        self._client = self._build_client()
        # Per-question response cache keyed by (kind, label, question,
        # options-tuple). Populated either by ``batch_prepare`` (one API
        # call for many questions) or as a side-effect of single calls,
        # so the same question is never billed twice in one job.
        self._cache: dict[tuple, object] = {}
        # Counters for observability -- exposed on the instance and
        # logged at end-of-job so the user can see the savings.
        self.api_calls = 0
        self.cache_hits = 0
        # Raw token counts accumulated across all API calls for this job.
        # Used to compute per-job cost.
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        # Distinct cache keys that were actually consumed during fill --
        # i.e. unique questions answered without a per-question API call.
        # Useful for reporting "saved N API calls" in a way that doesn't
        # double-count when the same question is looked up twice (e.g.
        # by a verify-and-refill pass).
        self.distinct_served: set[tuple] = set()
        # Job-specific context. The autobid sets this once per job so
        # the AI can tailor answers to the role the candidate is
        # applying for. Cleared between jobs because cached answers
        # are tied to the JD that was active when they were generated.
        self._job_description: str = ""
        # Per-job company-summary blurb scraped from jobright (when
        # available). Used as additional prompt grounding so generated
        # answers reflect the employer's mission / domain. Like
        # ``job_description``, changing it clears the cache.
        self._company_summary: str = ""

    @property
    def cost_usd(self) -> float:
        """Estimated API spend for this job based on accumulated token counts.

        Uses the standard-tier price table in :data:`_MODEL_PRICE_PER_1M`.
        Returns 0.0 for unknown models rather than raising.
        """
        price_in, price_out = _model_price(self.model)
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000

    @property
    def profile(self) -> ApplicantProfile:
        return self._profile

    @profile.setter
    def profile(self, value: ApplicantProfile) -> None:
        """Re-point the answerer at a different ApplicantProfile.

        The autobid pipeline calls this once per job after the
        per-job resume matcher picks the best-fit CV. If the new
        profile points at a different resume than the previous one,
        we clear the cached answers so the AI re-generates them
        grounded in the matched resume rather than the stale one.
        """

        prev = getattr(self, "_profile", None)
        prev_path = getattr(prev, "resume_path", None)
        prev_text = getattr(prev, "resume_text", "") or ""
        new_path = getattr(value, "resume_path", None)
        new_text = getattr(value, "resume_text", "") or ""
        if prev_path != new_path or prev_text != new_text:
            self._cache.clear()
            self.distinct_served.clear()
            self.api_calls = 0
            self.cache_hits = 0
            self.input_tokens = 0
            self.output_tokens = 0
        self._profile = value

    @property
    def job_description(self) -> str:
        return self._job_description

    @job_description.setter
    def job_description(self, value: str) -> None:
        """Set the per-job description text. Setting a new JD clears
        the answer cache because previously-cached answers were
        generated against a DIFFERENT job and would now be misleading.
        Counters are also reset so the per-job log line is accurate."""

        new_value = (value or "").strip()
        if new_value != self._job_description:
            self._cache.clear()
            self.distinct_served.clear()
            self.api_calls = 0
            self.cache_hits = 0
            self.input_tokens = 0
            self.output_tokens = 0
        self._job_description = new_value

    @property
    def company_summary(self) -> str:
        return self._company_summary

    @company_summary.setter
    def company_summary(self, value: str) -> None:
        """Set the per-job company-summary text. Like
        ``job_description``, switching to a different summary clears
        the answer cache so cached answers grounded in the previous
        company don't bleed into the new one.
        """

        new_value = (value or "").strip()
        if new_value != self._company_summary:
            self._cache.clear()
            self.distinct_served.clear()
            self.input_tokens = 0
            self.output_tokens = 0
        self._company_summary = new_value

    @classmethod
    def from_env(
        cls,
        profile: ApplicantProfile,
        project_root: Path | None = None,
    ) -> "AIAnswerer | None":
        """Build an answerer from environment variables, or ``None`` if the
        AI integration is disabled / not configured.
        """

        provider = os.getenv("AI_PROVIDER", "").lower().strip()
        if not provider:
            logger.info("AI_PROVIDER not set; AI question answering disabled.")
            return None

        if provider != "openai":
            logger.warning(
                "AI_PROVIDER=%r is not yet supported (only 'openai'); "
                "AI question answering disabled.",
                provider,
            )
            return None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "OPENAI_API_KEY is empty; AI question answering disabled."
            )
            return None

        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

        prompt_file_raw = os.getenv("AI_PROMPT_FILE", "ai_prompt.txt").strip()
        prompt_path = Path(prompt_file_raw)
        if not prompt_path.is_absolute():
            root = (project_root or Path.cwd()).resolve()
            prompt_path = root / prompt_path
        try:
            prompt_template = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not read AI prompt file %s: %s; using fallback prompt.",
                prompt_path,
                exc,
            )
            prompt_template = _DEFAULT_PROMPT

        return cls(
            provider=provider,
            api_key=api_key,
            model=model,
            prompt_template=prompt_template,
            profile=profile,
        )

    def answer(self, ctx: QuestionContext) -> str | None:
        """Generate an answer string for the given question, or None on
        error. Cached -- if ``batch_prepare`` already populated this
        question's answer, no API call is made."""

        cached = self._cache_get(ctx, "answer")
        if cached is not _CACHE_MISS:
            return cached if isinstance(cached, str) else None

        prompt = self._render_prompt(ctx)
        logger.debug("AI prompt for question %r:\n%s", ctx.label, prompt)
        text = self._call_text(prompt, label=ctx.label)
        if text:
            self._cache_set(ctx, "answer", text)
            logger.info("AI answer for %r: %s", ctx.label, _truncate(text, 200))
        return text

    def pick_option(self, ctx: QuestionContext) -> str | None:
        """For a multi-choice question, ask the AI to pick exactly ONE option."""

        if not ctx.options:
            return self.answer(ctx)

        cached = self._cache_get(ctx, "pick_option")
        if cached is not _CACHE_MISS:
            return cached if isinstance(cached, str) else None

        prompt = self._render_prompt(ctx)
        text = self._call_text(prompt, label=ctx.label)
        if not text:
            return None
        choice = _match_option(text, ctx.options)
        if not choice:
            logger.warning(
                "AI answer %r does not match any option in %s for %r; skipping.",
                text,
                list(ctx.options),
                ctx.label,
            )
            return None
        self._cache_set(ctx, "pick_option", choice)
        return choice

    def pick_multi(self, ctx: QuestionContext) -> tuple[str, ...]:
        """For a "select all that apply" question, ask the AI to return
        zero or more options. Returns a tuple of matched option labels."""

        if not ctx.options:
            return ()

        cached = self._cache_get(ctx, "pick_multi")
        if cached is not _CACHE_MISS:
            return cached if isinstance(cached, tuple) else ()

        # Add a hint so the AI knows multi-select is OK.
        multi_ctx = QuestionContext(
            label=ctx.label,
            question=(
                ctx.question
                + "\n\n(Pick all options that apply. Reply with the option "
                "labels separated by commas, or the single word NONE if "
                "no option applies.)"
            ),
            options=ctx.options,
        )
        prompt = self._render_prompt(multi_ctx)
        text = self._call_text(prompt, label=ctx.label)
        chosen = _parse_multi_response(text or "", ctx.options)
        self._cache_set(ctx, "pick_multi", chosen)
        return chosen

    # ------------------------------------------------------------------
    # Batched answering
    # ------------------------------------------------------------------

    def batch_prepare(self, items: list[BatchItem]) -> int:
        """Send all ``items`` in ONE API call and populate the cache
        with the parsed answers. Returns the number of items that were
        successfully cached.

        Items already present in the cache are silently skipped (so it's
        safe to call this repeatedly with overlapping question sets --
        e.g. a second batch for lazy-rendered fields).

        On any failure (network, malformed JSON, missing keys) the
        method falls back gracefully: nothing is cached for the failed
        items and the per-question API path will still answer them
        individually. We never silently corrupt the cache.
        """

        # Filter out anything we already have an answer for. Also dedupe
        # by canonical cache key so the same question doesn't show up
        # twice in the prompt.
        pending: list[BatchItem] = []
        seen_keys: set[tuple] = set()
        for item in items:
            key = self._key(item.ctx, item.kind)
            if key in self._cache or key in seen_keys:
                continue
            seen_keys.add(key)
            pending.append(item)

        if not pending:
            logger.debug("batch_prepare: nothing new to ask; cache already populated.")
            return 0

        prompt = self._render_batch_prompt(pending)
        logger.debug(
            "AI batch prompt for %d question(s):\n%s",
            len(pending),
            _truncate(prompt, 4000),
        )

        try:
            self.api_calls += 1
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                response_format={"type": "json_object"},
            )
            if response.usage:
                self.input_tokens += response.usage.prompt_tokens or 0
                self.output_tokens += response.usage.completion_tokens or 0
            raw = response.choices[0].message.content or ""
        except Exception as exc:  # network / auth / rate-limit / model
            logger.warning(
                "Batch AI call failed (%s); will fall back to per-question API calls.",
                exc,
            )
            return 0

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Batch AI response was not valid JSON (%s); raw=%r; falling back.",
                exc,
                _truncate(raw, 400),
            )
            return 0

        if not isinstance(data, dict):
            logger.warning("Batch AI returned non-object JSON %r; falling back.", type(data))
            return 0

        cached_count = 0
        for item in pending:
            if item.id not in data:
                logger.debug(
                    "Batch reply missing answer for item %r (label=%r); will retry per-question.",
                    item.id,
                    item.ctx.label,
                )
                continue
            value = data[item.id]
            stored = self._coerce_batch_value(item, value)
            if stored is None:
                continue
            self._cache_set(item.ctx, item.kind, stored)
            cached_count += 1

        logger.info(
            "Batch AI: 1 API call answered %d/%d question(s); %d question(s) "
            "left for per-question fallback.",
            cached_count,
            len(pending),
            len(pending) - cached_count,
        )
        return cached_count

    def _coerce_batch_value(self, item: BatchItem, value: object) -> object | None:
        """Convert a raw JSON value from the batch reply into the
        canonical type for ``item.kind``. Returns None if the value
        isn't usable (e.g. ``pick_option`` answer doesn't match any
        provided option)."""

        if item.kind == "answer":
            if isinstance(value, str):
                return value.strip().strip('"').strip() or None
            if isinstance(value, (int, float, bool)):
                return str(value)
            return None

        if item.kind == "pick_option":
            if not isinstance(value, str):
                return None
            choice = _match_option(value, item.ctx.options)
            if not choice:
                logger.warning(
                    "Batch answer %r for %r doesn't match any option in %s.",
                    value,
                    item.ctx.label,
                    list(item.ctx.options),
                )
            return choice

        if item.kind == "pick_multi":
            if isinstance(value, list):
                # Convert each entry to its canonical option label.
                chosen: list[str] = []
                seen: set[str] = set()
                for v in value:
                    if not isinstance(v, str):
                        continue
                    canon = _match_option(v, item.ctx.options)
                    if canon and canon.lower() not in seen:
                        chosen.append(canon)
                        seen.add(canon.lower())
                return tuple(chosen)
            if isinstance(value, str):
                # AI ignored the "list" instruction; parse comma list.
                return _parse_multi_response(value, item.ctx.options)
            return ()

        return None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(ctx: QuestionContext, kind: AnswerKind) -> tuple:
        return (kind, ctx.label, ctx.question, tuple(ctx.options))

    def _cache_get(self, ctx: QuestionContext, kind: AnswerKind) -> object:
        key = self._key(ctx, kind)
        if key in self._cache:
            self.cache_hits += 1
            self.distinct_served.add(key)
            return self._cache[key]
        return _CACHE_MISS

    def _cache_set(self, ctx: QuestionContext, kind: AnswerKind, value: object) -> None:
        if value is None:
            return
        if kind == "pick_multi" and not isinstance(value, tuple):
            value = tuple(value)
        self._cache[self._key(ctx, kind)] = value

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_client(self):
        # Imported lazily so the rest of the package works without the openai
        # SDK being installed (e.g. when AI_PROVIDER is unset).
        from openai import OpenAI

        return OpenAI(api_key=self.api_key)

    def _call_text(self, prompt: str, *, label: str) -> str | None:
        try:
            self.api_calls += 1
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
        except Exception as exc:  # network / auth / rate-limit -- non-fatal
            logger.warning("AI call failed for question %r: %s", label, exc)
            return None
        if response.usage:
            self.input_tokens += response.usage.prompt_tokens or 0
            self.output_tokens += response.usage.completion_tokens or 0
        text = (response.choices[0].message.content or "").strip().strip('"').strip()
        if not text:
            logger.warning("AI returned empty answer for question %r.", label)
            return None
        return text

    def _render_batch_prompt(self, items: list[BatchItem]) -> str:
        """Build a single prompt that asks the AI to answer many
        questions at once and return a JSON object keyed by item id."""

        location_parts = [self.profile.city, self.profile.state, self.profile.country]
        location = ", ".join(p for p in location_parts if p) or "Not specified"

        resume_text = (self.profile.resume_text or "").strip()
        if resume_text:
            resume_block = (
                "CANDIDATE RESUME (verbatim text from the uploaded CV; "
                "rely on this for any specific facts about experience, "
                "projects, employers, dates, or skills):\n"
                + resume_text
                + "\n\n"
            )
        else:
            resume_block = ""

        company_summary = self._company_summary
        if company_summary:
            company_block = (
                "COMPANY SUMMARY (a short blurb about the employer; use "
                "to set tone and naturally reference the company's "
                "mission / domain when relevant -- never invent extra "
                "facts not present here):\n"
                + company_summary
                + "\n\n"
            )
        else:
            company_block = ""

        jd = self._job_description
        if jd:
            jd_block = (
                "JOB DESCRIPTION (the specific role the candidate is "
                "applying for; tailor every answer to demonstrate "
                "relevance to this role's responsibilities and "
                "requirements):\n"
                + jd
                + "\n\n"
            )
        else:
            jd_block = ""

        kind_explainer = (
            "There are three answer types:\n"
            '  * "answer"      -- a free-text answer string.\n'
            '  * "pick_option" -- exactly ONE of the provided OPTIONS, '
            "copied verbatim.\n"
            '  * "pick_multi"  -- a JSON array of zero or more option '
            "labels, copied verbatim. Use [] when nothing applies.\n"
        )

        questions_block_lines: list[str] = []
        for item in items:
            header = f'[id="{item.id}", kind="{item.kind}"]'
            label_line = f'  Label: "{item.ctx.label}"'
            q_line = "  Question:\n    " + item.ctx.question.replace(
                "\n", "\n    "
            )
            block = [header, label_line, q_line]
            if item.ctx.options:
                block.append("  Options:")
                for opt in item.ctx.options:
                    block.append(f"    - {opt}")
            questions_block_lines.append("\n".join(block))
        questions_block = "\n\n".join(questions_block_lines)

        # Build a small JSON skeleton to encourage well-shaped output.
        skeleton: dict[str, object] = {}
        for item in items:
            if item.kind == "pick_multi":
                skeleton[item.id] = []
            else:
                skeleton[item.id] = ""
        skeleton_str = json.dumps(skeleton, indent=2)

        return (
            "SYSTEM:\n"
            "You are filling out one job application as the candidate "
            "described below. You will answer MULTIPLE application "
            "questions in a single response so we don't waste API "
            "tokens. Be concise, natural, professional, and written "
            "from the candidate's first-person perspective. No greetings, "
            "no signatures, no markdown.\n\n"
            "Length guidance for free-text answers:\n"
            "- Short factual question (yes/no, one-liner): 1 sentence (max ~30 words).\n"
            "- Standard short-answer question: 2-4 sentences.\n"
            '- Cover-letter-style / "tell us why you\'re a fit" question: 4-7 sentences, no fluff.\n\n'
            "Ground every answer in the CANDIDATE RESUME below. If a "
            "question asks for something the resume doesn't cover (e.g. "
            "specific years on a niche tool, salary expectation), pick a "
            "reasonable, conservative answer that does not disqualify "
            "the candidate; do not fabricate employers or dates that "
            "contradict the resume.\n\n"
            "CANDIDATE PREFERENCES (treat these as authoritative; "
            "never contradict them):\n"
            "- Availability: 40 hours per week, full-time.\n"
            "- Annual salary expectations: USD $120,000 \u2013 $130,000. "
            "If a single number is required, answer with $125,000.\n"
            "- Hourly rate expectations: USD $60 \u2013 $65. If a single "
            "number is required, answer with $62.\n"
            "- If the JOB DESCRIPTION quotes a specific salary or "
            "hourly range, IGNORE the figures above and answer with "
            "a value at the MIDDLE of that range (rounded to a clean "
            "number).\n"
            "- Work arrangement: fully remote ONLY. The candidate is "
            "NOT open to hybrid, on-site, or relocation.\n"
            "- Referral: the candidate was NOT referred by any "
            'current employee. If the question asks "who referred '
            'you?" or for a referrer\'s name, answer exactly "N/A".\n'
            "- Languages: proficient in American English only. Do "
            "not claim proficiency in any other language.\n\n"
            f"{kind_explainer}\n"
            "CANDIDATE PROFILE:\n"
            f"- Name: {self.profile.full_name or 'Candidate'}\n"
            f"- Email: {self.profile.email or 'n/a'}\n"
            f"- LinkedIn: {self.profile.linkedin or 'n/a'}\n"
            f"- Location: {location}\n"
            f"- Summary: {self.profile.summary or '(no summary provided)'}\n\n"
            f"{resume_block}"
            f"{company_block}"
            f"{jd_block}"
            f"QUESTIONS ({len(items)}):\n\n"
            f"{questions_block}\n\n"
            "ANSWER FORMAT:\n"
            "Reply with ONLY a JSON object. Keys are the question ids "
            "above; values follow the type rules. Do not include "
            "explanations or any text outside the JSON.\n\n"
            "Skeleton (fill these values):\n"
            f"{skeleton_str}\n"
        )

    def _render_prompt(self, ctx: QuestionContext) -> str:
        location_parts = [
            self.profile.city,
            self.profile.state,
            self.profile.country,
        ]
        location = ", ".join(p for p in location_parts if p) or "Not specified"

        if ctx.options:
            options_block = (
                "AVAILABLE OPTIONS (choose exactly ONE, copy its label "
                "verbatim with no extra text):\n"
                + "\n".join(f"- {o}" for o in ctx.options)
                + "\n"
            )
        else:
            options_block = ""

        resume_text = (self.profile.resume_text or "").strip()
        if resume_text:
            resume_block = (
                "CANDIDATE RESUME (verbatim text extracted from the "
                "uploaded CV; rely on this for any specific facts about "
                "experience, projects, employers, dates, or skills):\n"
                + resume_text
                + "\n"
            )
        else:
            resume_block = ""

        company_summary = self._company_summary
        if company_summary:
            company_summary_block = (
                "COMPANY SUMMARY (a short blurb about the employer; use "
                "to set tone and naturally reference the company's "
                "mission / domain when relevant -- never invent extra "
                "facts not present here):\n"
                + company_summary
                + "\n"
            )
        else:
            company_summary_block = ""

        jd = self._job_description
        if jd:
            jd_block = (
                "JOB DESCRIPTION (the specific role the candidate is "
                "applying for; tailor every answer to demonstrate "
                "relevance to this role's responsibilities and "
                "requirements):\n"
                + jd
                + "\n"
            )
        else:
            jd_block = ""

        format_kwargs: dict[str, str] = dict(
            full_name=self.profile.full_name or "Candidate",
            email=self.profile.email or "n/a",
            linkedin=self.profile.linkedin or "n/a",
            location=location,
            summary=self.profile.summary or "(no summary provided)",
            resume=resume_text or "(no resume text available)",
            resume_block=resume_block,
            company_summary=company_summary or "(no company summary available)",
            company_summary_block=company_summary_block,
            job_description=jd or "(no job description available)",
            job_description_block=jd_block,
            label=ctx.label or ctx.question,
            question=ctx.question,
            options_block=options_block,
        )
        try:
            return self.prompt_template.format(**format_kwargs)
        except KeyError as exc:
            logger.warning(
                "AI prompt template references unknown placeholder %s; "
                "falling back to default.",
                exc,
            )
            return _DEFAULT_PROMPT.format(**format_kwargs)


_DEFAULT_PROMPT = """\
You are filling out a job application as this candidate. Write the answer\
 in the candidate's first-person voice. Be concise, professional, and\
 honest. No greetings, no signatures, no markdown.

CANDIDATE:
- Name: {full_name}
- Email: {email}
- LinkedIn: {linkedin}
- Location: {location}
- Summary: {summary}

{resume_block}
{company_summary_block}
{job_description_block}
QUESTION (label = "{label}"):
{question}

{options_block}
ANSWER:
"""


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "\u2026"


# Sentinel used by ``_cache_get`` to distinguish "answer is missing"
# from "answer is None" (which the per-question code path uses to mean
# "the AI failed for this question; stop trying"). We can't use None
# because we need to be able to cache that None as a real answer.
_CACHE_MISS = object()


def _match_option(value: str, options: tuple[str, ...]) -> str | None:
    """Return the option whose label best matches ``value``. Tries
    case-insensitive exact, then case-insensitive substring (either
    direction). Returns ``None`` when nothing fits."""

    if not value or not options:
        return None
    v = value.strip().strip('"').strip()
    if not v:
        return None
    lc = v.lower()
    for opt in options:
        if opt.lower() == lc:
            return opt
    for opt in options:
        if opt.lower() in lc or lc in opt.lower():
            return opt
    return None


def _parse_multi_response(text: str, options: tuple[str, ...]) -> tuple[str, ...]:
    """Convert a free-form multi-pick AI reply (commas, newlines, bullets)
    into the canonical list of option labels."""

    cleaned = text.strip()
    if not cleaned or cleaned.lower() in ("none", "n/a", "no", "(none)", "[]"):
        return ()
    candidates = [
        c.strip(" -*\u2022\t\"'")
        for c in re.split(r"[,\n]", cleaned)
        if c.strip()
    ]
    chosen: list[str] = []
    seen_lower: set[str] = set()
    for cand in candidates:
        canon = _match_option(cand, options)
        if canon and canon.lower() not in seen_lower:
            chosen.append(canon)
            seen_lower.add(canon.lower())
    return tuple(chosen)


def iter_options(opts: Iterable[str]) -> tuple[str, ...]:
    """Convenience: dedupe + strip option labels for nicer prompts."""

    seen: list[str] = []
    for o in opts:
        s = (o or "").strip()
        if s and s not in seen:
            seen.append(s)
    return tuple(seen)
