"""Greezik entrypoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import applier, auth, browser
from .config import load_config
from .logging_setup import setup_logging
from .storage import AppliedURLStore

EXIT_OK = 0
EXIT_LOGIN_FAILED = 2
EXIT_UNHANDLED = 3


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    setup_logging(log_dir=project_root / "logs")
    logger = logging.getLogger("greezik")

    try:
        cfg = load_config(project_root=project_root)
    except RuntimeError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_UNHANDLED

    logger.info("Starting Greezik")
    logger.info("Profile dir: %s", cfg.browser_profile_dir)
    logger.info("Output file: %s", cfg.applied_urls_file)

    store = AppliedURLStore(cfg.applied_urls_file)

    try:
        with browser.launch_browser(
            cfg.browser_profile_dir,
            headless=False,
            action_timeout_ms=cfg.action_timeout_ms,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()

            auth.open_homepage_and_settle(page, cfg.post_load_wait_seconds)

            try:
                auth.ensure_signed_in(
                    page,
                    email=cfg.email,
                    password=cfg.password,
                    redirect_wait_seconds=cfg.redirect_wait_seconds,
                )
            except auth.LoginError as exc:
                logger.error("Login failed: %s", exc)
                return EXIT_LOGIN_FAILED

            stats = applier.run_apply_loop(
                context,
                page,
                store,
                action_timeout_ms=cfg.action_timeout_ms,
            )

            logger.info(
                "Done. captured=%d duplicates=%d skipped=%d "
                "modals_dismissed=%d total_in_store=%d",
                stats.captured,
                stats.duplicates,
                stats.skipped,
                stats.modals_dismissed,
                len(store),
            )
            return EXIT_OK

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return EXIT_OK
    except Exception:
        logger.exception("Unhandled error")
        return EXIT_UNHANDLED


if __name__ == "__main__":
    sys.exit(main())
