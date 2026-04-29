"""Browser bootstrap helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Playwright, sync_playwright


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@contextmanager
def launch_browser(
    profile_dir: Path,
    *,
    headless: bool = False,
    action_timeout_ms: int = 15_000,
) -> Iterator[BrowserContext]:
    """Launch a persistent Chromium context and yield it.

    The persistent profile directory keeps cookies/local storage between runs
    so we usually don't need to log in again after the first successful sign-in.
    """

    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = _launch(pw, profile_dir, headless=headless)
        context.set_default_timeout(action_timeout_ms)
        context.set_default_navigation_timeout(max(action_timeout_ms, 30_000))
        try:
            yield context
        finally:
            try:
                context.close()
            except Exception:
                pass


def _launch(pw: Playwright, profile_dir: Path, *, headless: bool) -> BrowserContext:
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        user_agent=DEFAULT_USER_AGENT,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
        ignore_default_args=["--enable-automation"],
    )
