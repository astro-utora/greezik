"""Main loop: iterate jobright job titles, scrape detail, autobid externally, confirm.

The flow each iteration:

1. Click the first ``<h2 class="...index_job-title__...">`` on the
   jobright job list -- this opens the detail panel and routes the URL
   to ``/jobs/info/<id>``.
2. Pull title / company / company-summary / salary / responsibilities /
   qualification bullets off the detail panel and persist them to
   ``bid/jobright/Job_Description.txt``.
3. Click the panel's Apply button to open the external (Greenhouse)
   apply popup. The autobid step then reads the saved JD and chooses
   the best resume from ``APPLICANT_RESUMES_DIR``, copying the chosen
   PDF to ``bid/jobright/resume.pdf`` for upload.
4. After the popup is closed (success / skip / failure), confirm the
   "Did you apply?" modal that jobright surfaces back on the list page.
5. Click the detail-panel X close button so the next iteration sees a
   clean job list, and repeat.

Every URL we route to is recorded -- ``applied_jobs.jsonl`` for
confirmed/manual submits, ``skipped_jobs.jsonl`` for non-Greenhouse
portals, judge SKIPs, recently-applied companies, and submit failures
the user dismissed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from . import autobid as autobid_mod
from . import jobright_jd, notifications, selectors
from .ai_answer import AIAnswerer
from .jobright_jd import JobrightJobDetails
from .profile import ApplicantProfile
from .storage import AppliedURLStore, ManualRequiredStore, SkippedURLStore

logger = logging.getLogger(__name__)


# Domain that signals "this URL is a Greenhouse application form we can
# autofill". Anything else is a portal we don't have a filler for; we
# log it to ``skipped_jobs.jsonl`` and move on.
_GREENHOUSE_HOST_NEEDLE = "job-boards.greenhouse.io"


@dataclass
class ApplyStats:
    captured: int = 0
    duplicates: int = 0
    skipped: int = 0
    modals_dismissed: int = 0
    submitted: int = 0
    autobid_failed: int = 0
    manual_required: int = 0


def run_apply_loop(
    context: BrowserContext,
    page: Page,
    store: AppliedURLStore,
    *,
    action_timeout_ms: int,
    skipped_store: SkippedURLStore | None = None,
    manual_required_store: ManualRequiredStore | None = None,
    profile: ApplicantProfile | None = None,
    ai: AIAnswerer | None = None,
    submit_greenhouse: bool = True,
    company_dedup_days: int = 3,
    manual_apply_alert: bool = True,
    project_root: Path | None = None,
) -> ApplyStats:
    """Iterate the jobright job list one detail-panel at a time."""

    if skipped_store is None:
        # Make the new path opt-in: callers that haven't yet been
        # updated still get the old behaviour (URL-only capture).
        logger.warning(
            "run_apply_loop called without a SkippedURLStore -- "
            "skipped jobs will not be persisted."
        )

    # Resolve once so every iteration writes ``Job_Description.txt`` and
    # ``resume.pdf`` to the same well-known folder regardless of where
    # Greezik was launched from.
    root = (project_root or Path.cwd()).resolve()

    stats = ApplyStats()
    iteration = 0
    consecutive_failures = 0
    max_consecutive_failures = 3

    if not _wait_for_job_title(page, timeout_ms=action_timeout_ms):
        logger.info("No job titles after initial wait; trying a scroll to load more.")
        _scroll_to_bottom(page)
        if not _wait_for_job_title(page, timeout_ms=action_timeout_ms):
            logger.info("No job titles available at all. Exiting.")
            return stats

    while True:
        iteration += 1
        logger.info("--- Apply iteration %d ---", iteration)

        # Self-heal: if a "Did you apply?" modal lingered from a previous
        # iteration (e.g. Jobright re-prompted, or our prior confirmation
        # didn't fully register), clear it now -- otherwise it blocks the
        # next click. ``dismiss_unwanted_modals`` deliberately leaves
        # this modal alone (it contains "Yes, I applied!" text), so we
        # have to handle it explicitly here.
        _click_yes_i_applied_if_present(page)

        # Same for the detail panel itself: if a previous iteration
        # bailed after extracting JD but before closing, the panel may
        # still be open. Close it so the H2 on the list is clickable.
        _close_job_detail_panel_if_open(page, action_timeout_ms=2_000)

        # Then sweep away any unwanted onboarding/promo modals before
        # interacting with the feed.
        stats.modals_dismissed += dismiss_unwanted_modals(page)

        if not _wait_for_job_title(page, timeout_ms=4_000):
            logger.info("No job titles found; one final scroll to load more.")
            _scroll_to_bottom(page)
            if not _wait_for_job_title(page, timeout_ms=4_000):
                logger.info("Still no job titles after scroll. Exiting loop.")
                break

        # Step 1: open the detail panel for the first job on the feed.
        try:
            details = _open_first_job_detail(
                page,
                action_timeout_ms=action_timeout_ms,
            )
        except _ApplyFailed as exc:
            consecutive_failures += 1
            stats.skipped += 1
            logger.warning(
                "Could not open detail panel (%s). Consecutive failures: %d/%d",
                exc,
                consecutive_failures,
                max_consecutive_failures,
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error("Too many consecutive open failures; aborting.")
                break
            stats.modals_dismissed += dismiss_unwanted_modals(page)
            _try_press_escape(page)
            time.sleep(1.0)
            continue

        # Step 2: persist the JD blob + the company summary as separate
        # files. Both are read back into memory so they can be threaded
        # through the apply log + AI prompt even if the disk goes
        # sideways later in the iteration.
        jd_path = jobright_jd.write_job_description_file(
            details, project_root=root
        )
        jd_text = jd_path.read_text(encoding="utf-8")
        summary_path = jobright_jd.write_company_summary_file(
            details, project_root=root
        )
        try:
            company_summary_text = summary_path.read_text(encoding="utf-8")
        except OSError:
            company_summary_text = details.to_company_summary_text()

        # Step 3: click Apply within the panel. This opens the external
        # popup we route through autobid. ALWAYS try to close the detail
        # panel before the next iteration, regardless of how this one
        # ends -- otherwise the panel stays on top of the list and the
        # next ``H2`` click is intercepted.
        try:
            try:
                popup = _click_apply_in_detail_and_get_popup(
                    context,
                    page,
                    action_timeout_ms=action_timeout_ms,
                )
            except _ApplyFailed as exc:
                consecutive_failures += 1
                stats.skipped += 1
                logger.warning(
                    "Apply click failed for %r (%s). Consecutive failures: %d/%d",
                    details.title or "(unknown role)",
                    exc,
                    consecutive_failures,
                    max_consecutive_failures,
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error("Too many consecutive apply failures; aborting.")
                    break
                stats.modals_dismissed += dismiss_unwanted_modals(page)
                continue

            consecutive_failures = 0

            # Step 4: route the popup. ``_handle_popup_for_url`` decides
            # autobid vs. skip-store; we always close the popup in the
            # ``finally`` regardless of what happens inside.
            outcome: _PopupOutcome
            try:
                url = _capture_popup_url(popup, action_timeout_ms=action_timeout_ms)
                if url is None:
                    outcome = _PopupOutcome(kind="unreachable", url="")
                else:
                    outcome = _handle_popup_for_url(
                        popup,
                        url,
                        iteration=iteration,
                        store=store,
                        skipped_store=skipped_store,
                        manual_required_store=manual_required_store,
                        profile=profile,
                        ai=ai,
                        submit_greenhouse=submit_greenhouse,
                        company_dedup_days=company_dedup_days,
                        manual_apply_alert=manual_apply_alert,
                        action_timeout_ms=action_timeout_ms,
                        details=details,
                        jd_text=jd_text,
                        company_summary_text=company_summary_text,
                        project_root=root,
                    )
            finally:
                _safe_close(popup)

            # Translate the popup outcome into stats + debug logging.
            if outcome.kind == "unreachable":
                logger.warning(
                    "Could not reach the apply URL; popup closed, continuing."
                )
                stats.skipped += 1
            elif outcome.kind == "duplicate_url":
                stats.duplicates += 1
                logger.info("Duplicate URL (already saved): %s", outcome.url)
            elif outcome.kind == "applied":
                stats.captured += 1
                stats.submitted += 1
                logger.info(
                    "Captured + submitted apply #%d: %s @ %s [%s]",
                    stats.captured,
                    outcome.role or "?",
                    outcome.company or "?",
                    outcome.url,
                )
            elif outcome.kind == "manual_applied":
                stats.captured += 1
                logger.info(
                    "Recorded manual apply #%d (user finished form): %s @ %s [%s]",
                    stats.captured,
                    outcome.role or "?",
                    outcome.company or "?",
                    outcome.url,
                )
            elif outcome.kind == "skipped":
                stats.skipped += 1
                logger.info(
                    "Skipped apply (%s): %s",
                    outcome.skip_reason or "unspecified",
                    outcome.url,
                )
            elif outcome.kind == "autobid_failed":
                stats.skipped += 1
                stats.autobid_failed += 1
                logger.warning(
                    "Autobid failed; logged as skipped (%s): %s",
                    outcome.skip_reason or "autobid_error",
                    outcome.url,
                )
            elif outcome.kind == "manual_required":
                stats.manual_required += 1
                logger.warning(
                    "Manual apply required (alert disabled): %s @ %s -- %s [%s]",
                    outcome.role or "?",
                    outcome.company or "?",
                    outcome.skip_reason or "submit_failed",
                    outcome.url,
                )

            # An unwanted modal can pop up between the popup closing and
            # the Yes-I-applied confirmation, blocking the click.
            stats.modals_dismissed += dismiss_unwanted_modals(page)
            _confirm_yes_i_applied(page, action_timeout_ms=action_timeout_ms)

        finally:
            # Step 5: close the detail panel so the next iteration sees
            # a fresh list. Do this in a finally so even an unexpected
            # raise inside the popup-handling block doesn't leave a
            # stuck panel.
            _close_job_detail_panel(page, action_timeout_ms=action_timeout_ms)

        # Give the feed a moment to render the next card.
        _wait_for_feed_settle(page, timeout_ms=4_000)

    return stats


# ---------------------------------------------------------------------------
# Per-popup routing: Greenhouse autobid vs. skip
# ---------------------------------------------------------------------------


@dataclass
class _PopupOutcome:
    """Internal result of handling one apply popup.

    ``kind`` drives the stats + logging in :func:`run_apply_loop`:

    * ``unreachable``      -- popup never loaded a real URL.
    * ``duplicate_url``    -- this exact URL was already on file.
    * ``applied``          -- Greenhouse form submitted by autobid.
    * ``manual_applied``   -- autobid couldn't submit but user clicked
                              OK on the manual-apply alert (they did it).
    * ``skipped``          -- non-Greenhouse, judge SKIP, dedup, or
                              user-cancelled manual alert.
    * ``autobid_failed``   -- autobid raised an unexpected exception.
    * ``manual_required``  -- submit didn't confirm AND
                              ``MANUAL_APPLY_ALERT=false``; logged to
                              ``manual_required.jsonl`` for later
                              human follow-up.
    """

    kind: str
    url: str
    role: str = ""
    company: str = ""
    skip_reason: str = ""


def _handle_popup_for_url(
    popup: Page,
    url: str,
    *,
    iteration: int,
    store: AppliedURLStore,
    skipped_store: SkippedURLStore | None,
    manual_required_store: ManualRequiredStore | None,
    profile: ApplicantProfile | None,
    ai: AIAnswerer | None,
    submit_greenhouse: bool,
    company_dedup_days: int,
    manual_apply_alert: bool,
    action_timeout_ms: int,
    details: JobrightJobDetails,
    jd_text: str,
    company_summary_text: str,
    project_root: Path,
) -> _PopupOutcome:
    """Dispatch a popup to autobid (Greenhouse) or skip-store (other).

    The autobid call is given the JD text we already extracted from
    jobright (``details`` + ``jd_text``) so it doesn't re-scrape the
    Greenhouse page; this also means ``role_title`` / ``company_name``
    on the saved record come from jobright, not from the Greenhouse
    page heuristics.
    """

    title_hint = details.title or ""
    company_hint = details.company or ""

    if store.has(url):
        return _PopupOutcome(
            kind="duplicate_url",
            url=url,
            role=title_hint,
            company=company_hint,
        )

    host = ""
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        host = ""

    is_greenhouse = _GREENHOUSE_HOST_NEEDLE in host

    if not is_greenhouse:
        # Non-Greenhouse portal: we don't have an autofill for it.
        # Log it to skipped_jobs.jsonl and let the human chase it.
        if skipped_store is not None:
            skipped_store.add(
                url,
                skip_reason="not_greenhouse",
                provider=host or "unknown",
                role_title=title_hint,
                company_name=company_hint,
            )
        logger.info("Non-Greenhouse apply portal (%s); skipping.", host or url)
        return _PopupOutcome(
            kind="skipped",
            url=url,
            role=title_hint,
            company=company_hint,
            skip_reason="not_greenhouse",
        )

    # 1. Per-company dedup window -- run BEFORE autobid so a duplicate
    #    company never even reaches the form fill / submit step. The
    #    employer name comes from the JobRight detail panel
    #    (``details.company``) which we already scraped above; the
    #    store normalizes it (case / punctuation / "Inc." suffixes) so
    #    e.g. "Higher Logic" and "Higher Logic, Inc." collapse.
    #
    #    The Greenhouse URL slug is still extracted -- it's logged on
    #    every JSONL record as ``company_slug`` for forensics -- but
    #    it no longer drives the dedup decision.
    early_slug = autobid_mod.extract_company_slug_from_url(url)
    if (
        company_hint
        and store.applied_to_company_within(company_hint, company_dedup_days)
    ):
        last = store.last_applied_to_company(company_hint)
        reason = (
            f"already_applied_within_{company_dedup_days}d"
            f" (last={last.isoformat() if last else 'unknown'})"
        )
        if skipped_store is not None:
            skipped_store.add(
                url,
                skip_reason=reason,
                provider="greenhouse",
                company_slug=early_slug,
                company_name=company_hint,
                role_title=title_hint,
            )
        logger.info(
            "Already applied to %r within last %d day(s); skipping "
            "before autobid runs.",
            company_hint,
            company_dedup_days,
        )
        return _PopupOutcome(
            kind="skipped",
            url=url,
            role=title_hint,
            company=company_hint or early_slug,
            skip_reason=reason,
        )

    # Greenhouse path. autobid wants the popup tab as its ``Page`` and
    # a fully-built ApplicantProfile + AIAnswerer.
    if profile is None:
        # Without a profile we can't safely fill anything; degrade to
        # capture-only so we don't drop the URL on the floor.
        store.add(
            url,
            provider="greenhouse",
            role_title=title_hint,
            company_name=company_hint,
            company_slug=early_slug,
        )
        return _PopupOutcome(
            kind="applied",
            url=url,
            role=title_hint,
            company=company_hint,
        )

    try:
        result = autobid_mod.autobid_apply(
            popup,
            profile,
            ai,
            submit=submit_greenhouse,
            action_timeout_ms=action_timeout_ms,
            jobright_jd_text=jd_text,
            jobright_company_summary=company_summary_text,
            jobright_role=title_hint,
            jobright_company=company_hint,
            jobright_bid_dir=(project_root / jobright_jd.BID_OUTPUT_DIR).resolve(),
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        logger.exception("Autobid raised on iteration %d: %s", iteration, exc)
        if skipped_store is not None:
            skipped_store.add(
                url,
                skip_reason=f"autobid_exception: {exc.__class__.__name__}",
                provider="greenhouse",
                company_slug=early_slug,
                role_title=title_hint,
                company_name=company_hint,
            )
        return _PopupOutcome(
            kind="autobid_failed",
            url=url,
            role=title_hint,
            company=company_hint,
            skip_reason=str(exc),
        )

    # ``role_title`` / ``company_name`` on ``result`` are sourced from
    # the jobright detail panel (passed via ``jobright_role`` /
    # ``jobright_company``), so they're already the values we want in
    # ``applied_jobs.jsonl``.
    role = result.role_title or title_hint
    company = result.company_name or company_hint
    slug = result.company_slug or early_slug

    # 2. Judge SKIP (low fit, blacklisted role, etc.).
    if result.skip_reason is not None:
        reason = f"judge_skip: {result.skip_reason}"
        if skipped_store is not None:
            skipped_store.add(
                url,
                skip_reason=reason,
                provider="greenhouse",
                company_slug=slug,
                company_name=company,
                role_title=role,
            )
        return _PopupOutcome(
            kind="skipped",
            url=url,
            role=role,
            company=company or slug,
            skip_reason=reason,
        )

    # 3. Submission outcome.
    status = result.submission_status
    if status in ("confirmed", "verified"):
        store.add(
            url,
            company_slug=slug,
            company_name=company,
            role_title=role,
            provider="greenhouse",
            submission_status=status,
            resume_used=(
                result.resume_used.name if result.resume_used else ""
            ),
        )
        return _PopupOutcome(
            kind="applied",
            url=url,
            role=role,
            company=company or slug,
        )

    if status == "not_attempted":
        # ``submit_greenhouse=False`` dry-run path. Treat as captured
        # so the same URL doesn't loop, but tag it so the human sees
        # the form was filled-only.
        store.add(
            url,
            company_slug=slug,
            company_name=company,
            role_title=role,
            provider="greenhouse",
            submission_status="filled_only",
            resume_used=(
                result.resume_used.name if result.resume_used else ""
            ),
        )
        return _PopupOutcome(
            kind="applied",
            url=url,
            role=role,
            company=company or slug,
        )

    # 4. Submit failed -- either pop the synchronous "manual apply"
    #    alert (default), or quietly record the job to
    #    ``manual_required.jsonl`` so the human can replay it later.
    head = _submit_failure_head(status)
    detail_lines = list(result.submission_errors[:5])
    if result.error and result.error not in detail_lines:
        detail_lines.insert(0, result.error)

    if not manual_apply_alert:
        if manual_required_store is not None:
            manual_required_store.add(
                url,
                status=status,
                error=result.error,
                submission_errors=list(result.submission_errors),
                provider="greenhouse",
                company_slug=slug,
                company_name=company,
                role_title=role,
                resume_used=(
                    result.resume_used.name if result.resume_used else ""
                ),
                head=head,
            )
        return _PopupOutcome(
            kind="manual_required",
            url=url,
            role=role,
            company=company or slug,
            skip_reason=f"submit_failed: {status}",
        )

    user_finished = notifications.manual_apply_alert(
        role=role,
        company=company or slug,
        url=url,
        head=head,
        detail_lines=detail_lines,
    )
    if user_finished:
        store.add(
            url,
            company_slug=slug,
            company_name=company,
            role_title=role,
            provider="greenhouse",
            submission_status="manual",
            resume_used=(
                result.resume_used.name if result.resume_used else ""
            ),
        )
        return _PopupOutcome(
            kind="manual_applied",
            url=url,
            role=role,
            company=company or slug,
        )

    # User clicked Cancel -> record as skipped.
    reason = f"submit_failed_user_skipped: {status}"
    if skipped_store is not None:
        skipped_store.add(
            url,
            skip_reason=reason,
            provider="greenhouse",
            company_slug=slug,
            company_name=company,
            role_title=role,
        )
    return _PopupOutcome(
        kind="skipped",
        url=url,
        role=role,
        company=company or slug,
        skip_reason=reason,
    )


def _submit_failure_head(status: str) -> str:
    if status == "needs_code_failed":
        return (
            "Greenhouse asked for an email verification code, but Greezik "
            "couldn't paste a valid one back."
        )
    if status == "form_error":
        return "Greenhouse rejected the submit -- visible field errors on the form."
    if status == "no_change":
        return (
            "Submit click had no visible effect -- the form may have failed "
            "silently."
        )
    return f"Submit outcome: {status}"


# ---------------------------------------------------------------------------
# Unwanted modal dismissal
# ---------------------------------------------------------------------------


def dismiss_unwanted_modals(page: Page) -> int:
    """Close any visible modal/drawer that isn't one of our known good ones.

    Identifies "good" modals (apply-customization, Yes-I-applied) by visible
    text and leaves them alone. For everything else, clicks the X close
    button if found, or falls back to pressing Escape.

    Returns the number of modals dismissed.
    """

    closed = 0
    overlays = page.locator(selectors.OVERLAY_CONTAINERS)
    try:
        count = overlays.count()
    except PlaywrightError:
        return 0

    for i in range(count):
        modal = overlays.nth(i)
        try:
            if not modal.is_visible():
                continue
            text = modal.inner_text(timeout=500)
        except PlaywrightError:
            continue

        if any(marker in text for marker in selectors.GOOD_MODAL_TEXT_MARKERS):
            logger.debug("Skipping known-good modal (preview: %r)", text[:60])
            continue

        preview = text.replace("\n", " | ").strip()[:120]
        close_btn = modal.locator(selectors.MODAL_CLOSE_SELECTORS).first
        try:
            close_btn.click(timeout=2_000)
            closed += 1
            logger.info("Dismissed unwanted modal via X (preview: %r)", preview)
            time.sleep(0.3)
            continue
        except PlaywrightError as exc:
            logger.debug("X close button not clickable (%s); trying Escape", exc)

        try:
            page.keyboard.press("Escape")
            closed += 1
            logger.info("Dismissed unwanted modal via Escape (preview: %r)", preview)
            time.sleep(0.3)
        except PlaywrightError as exc:
            logger.debug("Escape fallback failed: %s", exc)

    return closed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _ApplyFailed(Exception):
    pass


def _wait_for_job_title(page: Page, *, timeout_ms: int) -> bool:
    """Wait until at least one ``<h2>`` job title is visible on the feed."""
    try:
        page.locator(selectors.JOB_TITLE_LIST).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _wait_for_feed_settle(page: Page, *, timeout_ms: int) -> None:
    """After confirming an apply, wait for a job title to be visible again."""

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    time.sleep(0.4)
    remaining = max(int((deadline - time.monotonic()) * 1000), 500)
    _wait_for_job_title(page, timeout_ms=remaining)


def _open_first_job_detail(
    page: Page,
    *,
    action_timeout_ms: int,
) -> JobrightJobDetails:
    """Click the first ``<h2>`` job title and return the scraped detail."""

    short_timeout = min(action_timeout_ms, 6_000)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        title = page.locator(selectors.JOB_TITLE_LIST).first
        try:
            title.scroll_into_view_if_needed(timeout=short_timeout)
            visible_text = (title.inner_text(timeout=short_timeout) or "").strip()
        except PlaywrightError as exc:
            last_error = exc
            time.sleep(0.5)
            continue

        if visible_text:
            logger.info("Opening detail panel for job %r", visible_text)

        try:
            title.click(timeout=action_timeout_ms)
        except PlaywrightError as exc:
            last_error = exc
            msg = str(exc).lower()
            if (
                "not attached" in msg
                or "element is not stable" in msg
                or "element is not visible" in msg
            ) and attempt < 3:
                logger.warning(
                    "Title click attempt %d hit a detached/unstable element; "
                    "retrying with a fresh locator.",
                    attempt,
                )
                time.sleep(1.0)
                continue
            raise _ApplyFailed(f"Title click failed: {exc}") from exc

        if not jobright_jd.wait_for_detail_panel(
            page, timeout_ms=action_timeout_ms
        ):
            raise _ApplyFailed(
                "Detail panel never rendered after clicking job title."
            )

        # The DOM lazy-renders bullets a tick after the H1 appears, so
        # give the section bodies a moment before we scrape.
        jobright_jd.settle_after_url_change(page, seconds=0.8)

        details = jobright_jd.extract_job_details(page)
        if details.title or details.responsibilities or details.qualification_required:
            return details

        # Empty extract -- give the page one more beat in case it was
        # still streaming, then return whatever we get.
        time.sleep(0.7)
        return jobright_jd.extract_job_details(page)

    raise _ApplyFailed(
        f"Could not open job detail after retries: {last_error}"
    )


def _click_apply_in_detail_and_get_popup(
    context: BrowserContext,
    page: Page,
    *,
    action_timeout_ms: int,
) -> Page:
    """Click the panel's Apply button and return the resulting popup tab.

    Two interaction flows are possible (mirroring the legacy direct-from-
    feed click):

    1. Direct: clicking Apply opens a new tab.
    2. Modal:  clicking Apply opens an in-page customization modal; we
       then click "Apply without Customizing" inside another
       ``expect_page``.
    """

    short_timeout = min(action_timeout_ms, 6_000)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        apply_button = _detail_apply_button(page)
        # Diagnostic log so the next failure has the actual class /
        # text we matched -- if the bot still picks the wrong button
        # we'll know whether the selector or the page has changed.
        try:
            btn_text = (apply_button.inner_text(timeout=1_500) or "").strip()
            btn_class = apply_button.get_attribute("class") or ""
            btn_id = apply_button.get_attribute("id") or ""
            logger.info(
                "Apply button match: id=%r text=%r class=%r",
                btn_id,
                btn_text[:60],
                btn_class[:120],
            )
        except PlaywrightError as exc:
            logger.debug("Could not read apply-button metadata: %s", exc)

        try:
            apply_button.scroll_into_view_if_needed(timeout=short_timeout)
        except PlaywrightError:
            pass

        try:
            try:
                with context.expect_page(timeout=short_timeout) as popup_info:
                    apply_button.click(timeout=action_timeout_ms)
                return popup_info.value
            except PlaywrightTimeoutError:
                logger.debug("No popup yet; checking for modal fallback.")

            modal_button = page.locator(selectors.APPLY_WITHOUT_CUSTOMIZING).first
            try:
                modal_button.wait_for(state="visible", timeout=action_timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise _ApplyFailed(
                    "Apply click neither opened a popup nor a customization modal"
                ) from exc

            try:
                with context.expect_page(timeout=action_timeout_ms) as popup_info:
                    modal_button.click()
                return popup_info.value
            except PlaywrightTimeoutError as exc:
                raise _ApplyFailed(
                    "'Apply without Customizing' did not open a popup"
                ) from exc

        except PlaywrightError as exc:
            last_error = exc
            msg = str(exc).lower()
            if (
                "not attached" in msg
                or "element is not stable" in msg
                or "element is not visible" in msg
            ) and attempt < 3:
                logger.warning(
                    "Apply click attempt %d hit a detached/unstable element; "
                    "retrying with a fresh locator.",
                    attempt,
                )
                time.sleep(1.0)
                continue
            raise

    raise _ApplyFailed(f"Apply click failed after retries: {last_error}")


def _detail_apply_button(page: Page) -> Locator:
    """Locator for the Apply button on the open detail panel.

    Jobright wires the same stable ``id="apply-now-button-id"`` on every
    apply button it renders -- list cards (when the user is in card
    layout), the detail-panel sticky header, and any in-page reapply
    prompt all share that id. We rely on it exclusively because the
    legacy ``index_apply-button__<hash>`` class also appears on
    *unrelated* legacy widgets that are still in the DOM (e.g. the
    Overview tab's "Original Job Post" link), so a class-based fallback
    matches the wrong button and the click silently does nothing.

    When multiple buttons share the id, we pick the LAST visible one in
    document order: the detail panel is appended after the list in the
    DOM, so its button is reliably last.
    """

    by_id = page.locator("button#apply-now-button-id:visible")
    try:
        if by_id.count() > 0:
            return by_id.last
    except PlaywrightError:
        pass

    # Final fallback: ``selectors.APPLY_BUTTONS`` keeps the legacy /
    # camelCase class patterns for the rare account that hasn't rolled
    # out the new id yet. ``.last`` for the same DOM-order reasoning.
    return page.locator(selectors.APPLY_BUTTONS).last


def _close_job_detail_panel(
    page: Page, *, action_timeout_ms: int
) -> None:
    """Click the X button on the detail panel and wait for it to close."""

    button = page.locator(selectors.JOB_DETAIL_CLOSE_BUTTON).first
    try:
        if button.count() == 0 or not button.is_visible():
            return
    except PlaywrightError:
        return

    logger.info("Closing detail panel via X button.")
    for attempt in (1, 2):
        try:
            button.click(timeout=action_timeout_ms)
        except PlaywrightError as exc:
            logger.warning("Failed clicking detail-panel X (%s).", exc)
            return
        try:
            button.wait_for(state="hidden", timeout=4_000)
            return
        except PlaywrightTimeoutError:
            if attempt == 1:
                logger.warning("Detail panel still visible after X click; retrying.")
                time.sleep(0.4)
                continue
            logger.warning(
                "Detail panel persisted after two X clicks; continuing anyway."
            )


def _close_job_detail_panel_if_open(
    page: Page, *, action_timeout_ms: int
) -> None:
    """Best-effort: close the detail panel only if it's currently visible."""
    try:
        button = page.locator(selectors.JOB_DETAIL_CLOSE_BUTTON).first
        if button.count() == 0:
            return
        if not button.is_visible():
            return
    except PlaywrightError:
        return
    _close_job_detail_panel(page, action_timeout_ms=action_timeout_ms)


def _capture_popup_url(popup: Page, *, action_timeout_ms: int) -> str | None:
    """Wait for the popup to settle, then return its URL.

    Returns ``None`` if the popup never navigates beyond ``about:blank`` or
    lands on a Chrome error page (DNS failure, connection refused, etc.).
    Never raises -- the caller is expected to wrap this in a ``finally``
    that closes the popup regardless.
    """

    try:
        popup.wait_for_load_state("domcontentloaded", timeout=action_timeout_ms)
    except PlaywrightTimeoutError:
        logger.debug("Popup did not reach domcontentloaded in time.")
    except PlaywrightError as exc:
        logger.warning("Popup load_state errored (%s); continuing to capture URL.", exc)

    try:
        last_url = popup.url
    except PlaywrightError:
        return None

    deadline = time.monotonic() + min(action_timeout_ms, 8_000) / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        try:
            current = popup.url
        except PlaywrightError:
            break
        if current == last_url:
            break
        last_url = current

    if not last_url or last_url == "about:blank":
        return None

    if last_url.startswith("chrome-error://"):
        logger.warning("Popup landed on a Chrome error page; unreachable.")
        return None

    return last_url


def _safe_close(popup: Page) -> None:
    """Close a popup tab, swallowing all errors (it may already be closed)."""

    try:
        popup.close(run_before_unload=False)
    except PlaywrightError:
        pass
    except Exception:  # pragma: no cover - defensive belt-and-braces
        pass


def _click_yes_i_applied_if_present(page: Page) -> bool:
    """If the "Did you apply?" modal is currently visible, click its
    "Yes, I applied!" button to dismiss it.

    Used at the start of each iteration as a self-healing step: if the
    previous iteration's confirmation didn't fully clear (or Jobright
    re-prompted), the modal would block the next click.
    """

    button = page.get_by_role(
        "button", name=selectors.YES_I_APPLIED_TEXT, exact=True
    ).first
    try:
        if not button.is_visible():
            return False
    except PlaywrightError:
        return False

    logger.info("Clearing lingering 'Did you apply?' modal at iteration start.")
    try:
        button.click(timeout=4_000)
    except PlaywrightError as exc:
        logger.warning("Could not click lingering 'Yes, I applied!': %s", exc)
        return True

    try:
        button.wait_for(state="hidden", timeout=4_000)
    except PlaywrightTimeoutError:
        logger.warning(
            "Lingering 'Yes, I applied!' button still visible after click."
        )
    return True


def _confirm_yes_i_applied(page: Page, *, action_timeout_ms: int) -> None:
    """Click the green "Yes, I applied!" button on the "Did you apply?" modal."""

    primary = page.get_by_role(
        "button", name=selectors.YES_I_APPLIED_TEXT, exact=True
    )
    fallback = page.locator(
        f'button:has-text("{selectors.YES_I_APPLIED_TEXT}"):visible'
    )

    try:
        primary.first.wait_for(state="visible", timeout=action_timeout_ms)
        button = primary.first
    except PlaywrightTimeoutError:
        try:
            fallback.first.wait_for(state="visible", timeout=2_000)
            button = fallback.first
            logger.info("Using fallback text-based locator for 'Yes, I applied!'")
        except PlaywrightTimeoutError:
            logger.warning(
                "'Yes, I applied!' popup did not appear in time; continuing."
            )
            return

    logger.info("Clicking 'Yes, I applied!'")
    for attempt in (1, 2):
        try:
            button.click(timeout=action_timeout_ms)
        except PlaywrightError as exc:
            logger.warning("Failed clicking 'Yes, I applied!': %s", exc)
            return

        try:
            button.wait_for(state="hidden", timeout=4_000)
            logger.info("'Yes, I applied!' modal dismissed (attempt %d).", attempt)
            return
        except PlaywrightTimeoutError:
            if attempt == 1:
                logger.warning(
                    "'Yes, I applied!' button still visible after click; retrying."
                )
                time.sleep(0.5)
                continue
            logger.warning(
                "'Yes, I applied!' button persisted after two clicks; continuing."
            )


def _scroll_to_bottom(page: Page) -> None:
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
    except Exception:
        pass


def _try_press_escape(page: Page) -> None:
    """Best-effort recovery: press Escape to close any stuck overlay."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


