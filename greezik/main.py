"""Greezik entrypoint.

Two subcommands are supported:

``apply`` (default)
    The original auto-apply loop. Iterates the
    ``/jobs/recommend`` feed, autobids Greenhouse forms, and records
    each outcome to the configured JSONL files.

``backfill``
    Walk the ``/jobs/applied`` page top-to-bottom and append one
    record per applied job to ``APPLIED_URLS_FILE`` (default
    ``logs/jobright/big_log.jsonl``), clicking each card's X / remove
    button before moving on. Stops as soon as a card's publish time
    is older than ``BACKFILL_STOP_AFTER`` so the file ends up with
    only the recent slice the user actually cares about.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import applied_backfill, applier, auth, browser
from .ai_answer import AIAnswerer
from .config import Config, load_config
from .logging_setup import setup_logging
from .profile import load_profile
from .storage import AppliedURLStore, ManualRequiredStore, SkippedURLStore

EXIT_OK = 0
EXIT_LOGIN_FAILED = 2
EXIT_UNHANDLED = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="greezik",
        description=(
            "Automated jobright.ai apply bot. Default subcommand is "
            "'apply' (the auto-apply loop on /jobs/recommend); the "
            "'backfill' subcommand walks /jobs/applied and appends "
            "history records to APPLIED_URLS_FILE."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser(
        "apply",
        help="Run the auto-apply loop on /jobs/recommend (default).",
    )
    sub.add_parser(
        "backfill",
        help=(
            "Walk /jobs/applied and append the recent applied jobs to "
            "APPLIED_URLS_FILE. Stops at the first card older than "
            "BACKFILL_STOP_AFTER."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    project_root = Path(__file__).resolve().parent.parent
    setup_logging(log_dir=project_root / "logs")
    logger = logging.getLogger("greezik")

    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "apply"

    try:
        cfg = load_config(project_root=project_root)
    except RuntimeError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_UNHANDLED

    logger.info("Starting Greezik (command=%s)", command)
    logger.info(
        "Profile dir: %s (headless=%s)",
        cfg.browser_profile_dir,
        cfg.browser_headless,
    )

    if command == "backfill":
        return _run_backfill(cfg, logger=logger)
    return _run_apply(cfg, project_root=project_root, logger=logger)


# ---------------------------------------------------------------------------
# apply (default)
# ---------------------------------------------------------------------------


def _run_apply(
    cfg: Config, *, project_root: Path, logger: logging.Logger
) -> int:
    logger.info("Applied jobs file: %s", cfg.applied_urls_file)
    logger.info("Skipped jobs file: %s", cfg.skipped_urls_file)
    logger.info("Manual-required file: %s", cfg.manual_required_file)
    logger.info(
        "Company dedup window: %d day(s); applied-log retention: %d day(s); "
        "auto-submit Greenhouse: %s; manual-apply alert: %s",
        cfg.company_dedup_days,
        cfg.applied_log_retention_days,
        cfg.submit_greenhouse,
        cfg.manual_apply_alert,
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
    # Drop applied-job records older than the retention window so the
    # log doesn't grow unbounded across runs. A no-op when retention
    # is set to 0.
    applied_store.prune_older_than(cfg.applied_log_retention_days)
    skipped_store = SkippedURLStore(cfg.skipped_urls_file)
    # Always build the manual-required store so the file is created
    # on demand. The applier only writes to it when
    # ``manual_apply_alert`` is False.
    manual_required_store = ManualRequiredStore(cfg.manual_required_file)

    try:
        with browser.launch_browser(
            cfg.browser_profile_dir,
            headless=cfg.browser_headless,
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
                manual_required_store=manual_required_store,
                profile=profile,
                ai=ai,
                submit_greenhouse=cfg.submit_greenhouse,
                company_dedup_days=cfg.company_dedup_days,
                manual_apply_alert=cfg.manual_apply_alert,
                project_root=project_root,
            )

            logger.info(
                "Done. captured=%d submitted=%d duplicates=%d skipped=%d "
                "modals_dismissed=%d autobid_failed=%d manual_required=%d "
                "total_in_store=%d",
                stats.captured,
                stats.submitted,
                stats.duplicates,
                stats.skipped,
                stats.modals_dismissed,
                stats.autobid_failed,
                stats.manual_required,
                len(applied_store),
            )
            return EXIT_OK

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return EXIT_OK
    except Exception:
        logger.exception("Unhandled error")
        return EXIT_UNHANDLED


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def _run_backfill(cfg: Config, *, logger: logging.Logger) -> int:
    logger.info(
        "Backfill -> %s (stop_after=%r)",
        cfg.applied_urls_file,
        cfg.backfill_stop_after,
    )

    try:
        with browser.launch_browser(
            cfg.browser_profile_dir,
            headless=cfg.browser_headless,
            action_timeout_ms=cfg.action_timeout_ms,
            proxy=cfg.proxy,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()

            auth.open_homepage_and_settle(page, cfg.post_load_wait_seconds)

            try:
                # ``ensure_signed_in`` lands us on /jobs/recommend even
                # for the backfill flow -- the run_backfill_loop call
                # below navigates the same tab to /jobs/applied
                # immediately afterwards.
                auth.ensure_signed_in(
                    page,
                    email=cfg.email,
                    password=cfg.password,
                    redirect_wait_seconds=cfg.redirect_wait_seconds,
                )
            except auth.LoginError as exc:
                logger.error("Login failed: %s", exc)
                return EXIT_LOGIN_FAILED

            stats = applied_backfill.run_backfill_loop(
                page,
                applied_file=cfg.applied_urls_file,
                stop_after_spec=cfg.backfill_stop_after,
                action_timeout_ms=cfg.action_timeout_ms,
            )

            logger.info(
                "Backfill complete. recorded=%d removed=%d unparsed_publish=%d",
                stats.recorded,
                stats.removed,
                stats.unparsed_publish,
            )
            return EXIT_OK

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return EXIT_OK
    except Exception:
        logger.exception("Unhandled error during backfill")
        return EXIT_UNHANDLED


if __name__ == "__main__":
    sys.exit(main())
