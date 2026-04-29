"""Main loop: iterate Apply buttons, capture external URLs, confirm."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    ElementHandle,
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from . import autobid as autobid_mod
from . import notifications, selectors
from .ai_answer import AIAnswerer
from .profile import ApplicantProfile
from .storage import AppliedURLStore, SkippedURLStore

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


def run_apply_loop(
    context: BrowserContext,
    page: Page,
    store: AppliedURLStore,
    *,
    action_timeout_ms: int,
    skipped_store: SkippedURLStore | None = None,
    profile: ApplicantProfile | None = None,
    ai: AIAnswerer | None = None,
    submit_greenhouse: bool = True,
    company_dedup_days: int = 3,
) -> ApplyStats:
    """Click Apply buttons until none remain.

    For each external URL we open we route as follows:

    * **Greenhouse** (``job-boards.greenhouse.io``) -- run :mod:`autobid`
      against the popup. On a confirmed/verified submit, record to
      ``applied_jobs.jsonl``. On a judge-SKIP, dedup-skip, or user-
      dismissed submit failure, record to ``skipped_jobs.jsonl``.
    * **Anything else** -- save to ``skipped_jobs.jsonl`` with reason
      ``"not_greenhouse"`` and close the tab.

    The page is expected to already be on /jobs/recommend.
    """

    if skipped_store is None:
        # Make the new path opt-in: callers that haven't yet been
        # updated still get the old behaviour (URL-only capture).
        logger.warning(
            "run_apply_loop called without a SkippedURLStore -- "
            "skipped jobs will not be persisted."
        )

    stats = ApplyStats()
    iteration = 0
    consecutive_failures = 0
    max_consecutive_failures = 3

    # Wait for the feed to render before the first iteration (the recommend
    # page is fast on cached profiles but the job cards load a moment later).
    if not _wait_for_apply_button(page, timeout_ms=action_timeout_ms):
        logger.info("No apply buttons after initial wait; trying a scroll to load more.")
        _scroll_to_bottom(page)
        if not _wait_for_apply_button(page, timeout_ms=action_timeout_ms):
            logger.info("No apply buttons available at all. Exiting.")
            return stats

    while True:
        iteration += 1
        logger.info("--- Apply iteration %d ---", iteration)

        # Self-heal: if a "Did you apply?" modal lingered from a previous
        # iteration (e.g. Jobright re-prompted, or our prior confirmation
        # didn't fully register), clear it now -- otherwise it blocks the
        # next Apply click. ``dismiss_unwanted_modals`` deliberately leaves
        # this modal alone (it contains "Yes, I applied!" text), so we have
        # to handle it explicitly here.
        _click_yes_i_applied_if_present(page)

        # Then sweep away any unwanted onboarding/promo modals before
        # interacting with the feed.
        stats.modals_dismissed += dismiss_unwanted_modals(page)

        if not _wait_for_apply_button(page, timeout_ms=4_000):
            logger.info("No apply buttons found; attempting one final scroll to load more.")
            _scroll_to_bottom(page)
            if not _wait_for_apply_button(page, timeout_ms=4_000):
                logger.info("Still no apply buttons after scroll. Exiting loop.")
                break

        try:
            popup, applied_handle = _click_apply_and_get_popup(
                context,
                page,
                action_timeout_ms=action_timeout_ms,
            )
        except _ApplyFailed as exc:
            consecutive_failures += 1
            stats.skipped += 1
            logger.warning(
                "Apply attempt failed (%s). Consecutive failures: %d/%d",
                exc,
                consecutive_failures,
                max_consecutive_failures,
            )
            _save_debug_screenshot(page, f"apply_fail_iter{iteration}")
            if consecutive_failures >= max_consecutive_failures:
                logger.error("Too many consecutive apply failures; aborting.")
                break
            stats.modals_dismissed += dismiss_unwanted_modals(page)
            _try_press_escape(page)
            time.sleep(1.0)
            continue

        consecutive_failures = 0

        # GUARANTEE the popup tab is closed even if URL capture / autobid
        # throws -- otherwise unreachable / errored apply pages would
        # pile up as open tabs in the persistent Chromium profile.
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
                    profile=profile,
                    ai=ai,
                    submit_greenhouse=submit_greenhouse,
                    company_dedup_days=company_dedup_days,
                    action_timeout_ms=action_timeout_ms,
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
            _save_debug_screenshot(page, f"duplicate_url_iter{iteration}")
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

        # An unwanted modal can also pop up between the popup closing and
        # the Yes-I-applied confirmation appearing, blocking the click.
        stats.modals_dismissed += dismiss_unwanted_modals(page)
        _confirm_yes_i_applied(page, action_timeout_ms=action_timeout_ms)

        # Wait for THIS iteration's apply button to detach from the DOM
        # (Jobright removes the just-applied card asynchronously after the
        # confirmation). Without this, the next iteration races with the
        # feed update and clicks the same card again.
        _wait_for_handle_detached(applied_handle, timeout_ms=8_000)

        # Then give the feed a moment to render the next card.
        _wait_for_feed_settle(page, timeout_ms=4_000)

    return stats


# ---------------------------------------------------------------------------
# Per-popup routing: Greenhouse autobid vs. skip
# ---------------------------------------------------------------------------


@dataclass
class _PopupOutcome:
    """Internal result of handling one apply popup.

    ``kind`` drives the stats + logging in :func:`run_apply_loop`:

    * ``unreachable``    -- popup never loaded a real URL.
    * ``duplicate_url``  -- this exact URL was already on file.
    * ``applied``        -- Greenhouse form submitted by autobid.
    * ``manual_applied`` -- autobid couldn't submit but user clicked
                            OK on the manual-apply alert (they did it).
    * ``skipped``        -- non-Greenhouse, judge SKIP, dedup, or
                            user-cancelled manual alert.
    * ``autobid_failed`` -- autobid raised an unexpected exception.
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
    profile: ApplicantProfile | None,
    ai: AIAnswerer | None,
    submit_greenhouse: bool,
    company_dedup_days: int,
    action_timeout_ms: int,
) -> _PopupOutcome:
    """Dispatch a popup to autobid (Greenhouse) or skip-store (other).

    Always returns -- the caller closes the popup in its ``finally``
    regardless of what happens here. Exceptions inside autobid are
    caught so a single bad form doesn't abort the whole feed loop.
    """

    if store.has(url):
        return _PopupOutcome(kind="duplicate_url", url=url)

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
            )
        logger.info("Non-Greenhouse apply portal (%s); skipping.", host or url)
        return _PopupOutcome(
            kind="skipped",
            url=url,
            skip_reason="not_greenhouse",
        )

    # 1. Per-company dedup window -- run BEFORE autobid so a duplicate
    #    company never even reaches the form fill / submit step. The
    #    Greenhouse company slug lives in the URL's ``?for=...`` param,
    #    so we don't need to load the page to know who's hiring.
    early_slug = autobid_mod.extract_company_slug_from_url(url)
    if (
        early_slug
        and store.applied_to_company_within(early_slug, company_dedup_days)
    ):
        last = store.last_applied_to_company(early_slug)
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
            )
        logger.info(
            "Already applied to %r within last %d day(s); skipping "
            "before autobid runs.",
            early_slug,
            company_dedup_days,
        )
        return _PopupOutcome(
            kind="skipped",
            url=url,
            company=early_slug,
            skip_reason=reason,
        )

    # Greenhouse path. autobid wants the popup tab as its ``Page`` and
    # a fully-built ApplicantProfile + AIAnswerer.
    if profile is None:
        # Without a profile we can't safely fill anything; degrade to
        # capture-only so we don't drop the URL on the floor.
        store.add(url, provider="greenhouse")
        return _PopupOutcome(
            kind="applied",
            url=url,
        )

    try:
        result = autobid_mod.autobid_apply(
            popup,
            profile,
            ai,
            submit=submit_greenhouse,
            action_timeout_ms=action_timeout_ms,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        logger.exception("Autobid raised on iteration %d: %s", iteration, exc)
        if skipped_store is not None:
            skipped_store.add(
                url,
                skip_reason=f"autobid_exception: {exc.__class__.__name__}",
                provider="greenhouse",
                company_slug=early_slug,
            )
        return _PopupOutcome(
            kind="autobid_failed",
            url=url,
            skip_reason=str(exc),
        )

    role = result.role_title or ""
    company = result.company_name or ""
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

    # 4. Submit failed -- ask the human.
    head = _submit_failure_head(status)
    detail_lines = list(result.submission_errors[:5])
    if result.error and result.error not in detail_lines:
        detail_lines.insert(0, result.error)
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


def _wait_for_apply_button(page: Page, *, timeout_ms: int) -> bool:
    """Wait until at least one apply button is visible. Returns True if so."""

    try:
        page.locator(selectors.APPLY_BUTTONS).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _wait_for_feed_settle(page: Page, *, timeout_ms: int) -> None:
    """After confirming an apply, wait for an apply button to be visible again.

    The recommend feed is reactive; the just-applied card is removed and a
    new one slides in. Waiting for at least one apply button to be visible
    again gives the DOM a chance to settle so the next iteration's locator
    resolves to an attached element.
    """

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    # Small initial breather so React has a chance to start re-rendering.
    time.sleep(0.4)
    remaining = max(int((deadline - time.monotonic()) * 1000), 500)
    _wait_for_apply_button(page, timeout_ms=remaining)


def _click_apply_and_get_popup(
    context: BrowserContext,
    page: Page,
    *,
    action_timeout_ms: int,
) -> tuple[Page, ElementHandle | None]:
    """Click the first Apply button and return ``(popup_page, apply_button_handle)``.

    The handle lets the caller wait for the just-clicked card to be removed
    from the DOM before starting the next iteration; otherwise the feed
    update races with the next ``apply_button.click()`` and the same card
    is clicked twice.

    Two interaction flows are possible:
      1. Direct: clicking Apply opens a new tab (handled by ``expect_page``).
      2. Modal:  clicking Apply opens an in-page modal; we then click
         "Apply without Customizing" inside another ``expect_page``.

    The recommend feed mutates rapidly (cards re-shuffle), so we re-query the
    locator on each attempt and retry on detach/instability errors.
    """

    short_timeout = min(action_timeout_ms, 6_000)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        # Re-query each attempt to resolve to a currently-attached element.
        apply_button = page.locator(selectors.APPLY_BUTTONS).first

        # Capture an ElementHandle BEFORE the click so we can later wait for
        # it to detach. element_handle() resolves the locator now; if the
        # element disappears before/during the click we'll fall through to
        # the retry below.
        try:
            apply_handle: ElementHandle | None = apply_button.element_handle(
                timeout=4_000
            )
        except PlaywrightError:
            apply_handle = None

        try:
            # Direct popup path. Locator.click() auto-scrolls and auto-retries
            # on instability up to its own timeout, so we don't need an
            # explicit scroll_into_view_if_needed here.
            try:
                with context.expect_page(timeout=short_timeout) as popup_info:
                    apply_button.click(timeout=action_timeout_ms)
                return popup_info.value, apply_handle
            except PlaywrightTimeoutError:
                logger.debug("No popup yet; checking for modal fallback.")

            # Modal fallback path.
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
                return popup_info.value, apply_handle
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

    raise _ApplyFailed(
        f"Apply click failed after retries: {last_error}"
    )


def _wait_for_handle_detached(
    handle: ElementHandle | None, *, timeout_ms: int
) -> bool:
    """Block until ``handle``'s element is removed from the DOM.

    Returns True if detachment was confirmed within ``timeout_ms``. We poll
    via ``evaluate`` because ``ElementHandle.wait_for_element_state`` doesn't
    have a "detached" state. Returns True if the handle is None.
    """

    if handle is None:
        return True

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            connected = handle.evaluate("el => el.isConnected")
        except PlaywrightError:
            # Throws once the handle's owner frame is gone or the element is
            # disposed -- both indicate detachment.
            return True
        if not connected:
            return True
        time.sleep(0.2)
    logger.warning(
        "Previous apply card did not detach within %.1fs; feed may be stuck.",
        timeout_ms / 1000.0,
    )
    return False


def _capture_popup_url(popup: Page, *, action_timeout_ms: int) -> str | None:
    """Wait for the popup to settle, then return its URL.

    Returns ``None`` if the popup never navigates beyond ``about:blank`` or
    lands on a Chrome error page (DNS failure, connection refused, etc.).
    Never raises -- the caller is expected to wrap this in a ``finally``
    that closes the popup regardless.
    """

    # Attempt to wait for DOM ready. Tolerate any Playwright error here
    # (timeouts, navigation failures, target-closed) since the popup tab
    # still needs to be closed afterwards.
    try:
        popup.wait_for_load_state("domcontentloaded", timeout=action_timeout_ms)
    except PlaywrightTimeoutError:
        logger.debug("Popup did not reach domcontentloaded in time.")
    except PlaywrightError as exc:
        logger.warning("Popup load_state errored (%s); continuing to capture URL.", exc)

    # Read URL defensively.
    try:
        last_url = popup.url
    except PlaywrightError:
        return None

    # Give the popup a brief window to perform any client-side redirect,
    # but only if it isn't already at a useless URL.
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

    # Chrome shows error pages at scheme ``chrome-error://``. The intended
    # URL is gone at this point, so we can't save it -- treat as unreachable.
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

    This is used at the start of each iteration as a self-healing step: if
    the previous iteration's confirmation didn't fully clear (or Jobright
    re-prompted), the modal would block the next Apply click. Returns True
    if the modal was found and a click was attempted.
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
    """Click the green "Yes, I applied!" button on the "Did you apply?" modal.

    Verifies the click actually dismissed the modal; if not, retries once.
    """

    # Match by role+name (most resilient) but, as a fallback, also recognize a
    # plain button containing the visible text. Inside the apply-confirm modal
    # this is unique.
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


def _save_debug_screenshot(page: Page, label: str) -> None:
    try:
        out_dir = Path("logs")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{label}-{stamp}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.warning("Saved debug screenshot to %s", path)
    except Exception as exc:
        logger.debug("Could not save debug screenshot: %s", exc)
