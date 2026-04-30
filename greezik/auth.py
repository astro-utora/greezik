"""Sign-in flow and login state detection for jobright.ai."""

from __future__ import annotations

import logging
import time

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from . import selectors

logger = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """Raised when we cannot reach the recommended jobs page."""


def is_on_recommend(page: Page) -> bool:
    return selectors.RECOMMEND_PATH in page.url


def open_homepage_and_settle(page: Page, post_load_wait_seconds: float) -> None:
    """Navigate to the homepage and wait the configured settle time."""

    logger.info("Opening %s", selectors.HOMEPAGE_URL)
    page.goto(selectors.HOMEPAGE_URL, wait_until="domcontentloaded")
    if post_load_wait_seconds > 0:
        logger.debug("Waiting %.2fs after initial load", post_load_wait_seconds)
        time.sleep(post_load_wait_seconds)


def ensure_signed_in(
    page: Page,
    *,
    email: str,
    password: str,
    redirect_wait_seconds: float,
) -> None:
    """Make sure we end up on /jobs/recommend, signing in if needed.

    Decision flow after the initial settle wait:
      1. Already on /jobs/recommend? -> done.
      2. A visible SIGN IN trigger exists? -> user is not signed in; sign in.
      3. No SIGN IN trigger and not on /jobs/recommend? -> the auto-redirect
         probably hasn't completed yet (e.g. persistent-profile login restoring
         from cookies). Wait up to ``redirect_wait_seconds``.
      4. Still not on /jobs/recommend after the extra wait -> sign-in flow,
         re-locating a now-visible trigger.
    """

    logger.info("Current URL after initial settle: %s", page.url)

    if is_on_recommend(page):
        logger.info("Already on /jobs/recommend. Skipping sign-in.")
        return

    trigger = _find_visible_sign_in_trigger(page)
    if trigger is None:
        logger.info(
            "No SIGN IN trigger visible and not on /jobs/recommend yet; "
            "waiting up to %.1fs for auto-redirect (persistent session).",
            redirect_wait_seconds,
        )
        try:
            page.wait_for_url(
                f"**{selectors.RECOMMEND_PATH}**",
                timeout=int(redirect_wait_seconds * 1000),
            )
            logger.info("Auto-redirected to %s. Skipping sign-in.", page.url)
            return
        except PlaywrightTimeoutError:
            logger.info(
                "No auto-redirect after %.1fs. Re-checking for SIGN IN trigger.",
                redirect_wait_seconds,
            )
            trigger = _find_visible_sign_in_trigger(page)
            if trigger is None:
                raise LoginError(
                    "Could not find /jobs/recommend nor a visible 'SIGN IN' "
                    "trigger after waiting. Current URL: " + page.url
                )

    logger.info("Performing sign-in (trigger visible).")
    _perform_sign_in(page, trigger=trigger, email=email, password=password)

    try:
        page.wait_for_url(
            f"**{selectors.RECOMMEND_PATH}**",
            timeout=int(redirect_wait_seconds * 1000),
        )
    except PlaywrightTimeoutError as exc:
        raise LoginError(
            "Timed out waiting for redirect to /jobs/recommend after sign-in. "
            "Check credentials, captcha, or 2FA."
        ) from exc

    logger.info("Sign-in successful, on %s", page.url)


def _perform_sign_in(
    page: Page,
    *,
    trigger: Locator,
    email: str,
    password: str,
) -> None:
    logger.debug("Clicking SIGN IN trigger")
    trigger.click()

    logger.debug("Waiting for sign-in modal email input")
    email_input = page.locator(selectors.EMAIL_INPUT)
    try:
        email_input.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        # Sometimes the first click hits the span before the click handler is
        # attached; retry once on the same trigger.
        logger.debug("Email input did not appear; clicking SIGN IN once more")
        trigger.click()
        email_input.wait_for(state="visible", timeout=10_000)

    logger.debug("Filling credentials")
    email_input.fill(email)

    password_input = page.locator(selectors.PASSWORD_INPUT)
    password_input.wait_for(state="visible")
    password_input.fill(password)

    submit = page.locator(selectors.SIGN_IN_SUBMIT).first
    submit.wait_for(state="visible")
    submit.click()


def _find_visible_sign_in_trigger(page: Page) -> Locator | None:
    """Return the first visible element whose own text equals exactly 'SIGN IN'.

    Using ``get_by_text(..., exact=True)`` avoids two pitfalls of a CSS
    ``:has-text("SIGN IN")`` selector: case-insensitive matching (which would
    also catch the footer's "sign In" link) and substring matching on
    descendant text (which would catch outer wrapper elements).
    """

    candidates = page.get_by_text(selectors.SIGN_IN_TEXT, exact=True)
    try:
        total = candidates.count()
    except Exception:
        total = 0

    for i in range(total):
        el = candidates.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


