"""Greezik entrypoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import applier, auth, browser
from .ai_answer import AIAnswerer
from .config import load_config
from .logging_setup import setup_logging
from .profile import load_profile
from .storage import AppliedURLStore, SkippedURLStore

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
    logger.info("Applied jobs file: %s", cfg.applied_urls_file)
    logger.info("Skipped jobs file: %s", cfg.skipped_urls_file)
    logger.info(
        "Company dedup window: %d day(s); auto-submit Greenhouse: %s",
        cfg.company_dedup_days,
        cfg.submit_greenhouse,
    )

    # ApplicantProfile + AIAnswerer are built once and reused for
    # every Greenhouse popup we route through autobid.
    try:
        profile = load_profile(project_root=project_root)
    except RuntimeError as exc:
        logger.error("Profile error: %s", exc)
        return EXIT_UNHANDLED

    try:
        ai = AIAnswerer.from_env(profile, project_root=project_root)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not initialise the AI answerer (%s); free-form "
            "answers will fall back to safe defaults.",
            exc,
        )
        ai = None

    applied_store = AppliedURLStore(cfg.applied_urls_file)
    skipped_store = SkippedURLStore(cfg.skipped_urls_file)

    try:
        with browser.launch_browser(
            cfg.browser_profile_dir,
            headless=False,
            action_timeout_ms=cfg.action_timeout_ms,
            proxy=cfg.proxy,
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
                applied_store,
                action_timeout_ms=cfg.action_timeout_ms,
                skipped_store=skipped_store,
                profile=profile,
                ai=ai,
                submit_greenhouse=cfg.submit_greenhouse,
                company_dedup_days=cfg.company_dedup_days,
            )

            logger.info(
                "Done. captured=%d submitted=%d duplicates=%d skipped=%d "
                "modals_dismissed=%d autobid_failed=%d total_in_store=%d",
                stats.captured,
                stats.submitted,
                stats.duplicates,
                stats.skipped,
                stats.modals_dismissed,
                stats.autobid_failed,
                len(applied_store),
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
