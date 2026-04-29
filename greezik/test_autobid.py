"""Test CLI for the autobid module.

Reads URLs from ``applied_jobs.jsonl`` (or any JSONL file passed via
``--source``), filters to Greenhouse URLs by default, opens each in a fresh
Chromium tab, fills the form using your ``.env`` profile, and either leaves
it open for manual review (default) or actually submits (``--submit``).

Usage:
  python -m greezik.test_autobid                          # all greenhouse URLs, dry run
  python -m greezik.test_autobid --limit 1                # just the first one
  python -m greezik.test_autobid --url <greenhouse_url>   # ad-hoc URL
  python -m greezik.test_autobid --submit --limit 1       # actually submit ONE
  python -m greezik.test_autobid --hold 30                # keep window open 30s after fill
  python -m greezik.test_autobid --no-review              # skip the per-job review prompt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import autobid
from .ai_answer import AIAnswerer
from .config import load_config
from .logging_setup import setup_logging
from .profile import load_profile

logger = logging.getLogger("greezik.test_autobid")


def _read_jsonl_urls(path: Path) -> list[str]:
    urls: list[str] = []
    if not path.exists():
        logger.error("Source JSONL %s does not exist.", path)
        return urls
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = rec.get("external_url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _filter_supported(urls: list[str]) -> list[str]:
    return [u for u in urls if autobid.is_supported_url(u)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Greezik autobid test runner")
    parser.add_argument(
        "--source",
        default="applied_jobs.jsonl",
        help="JSONL file with previously captured apply URLs (default: applied_jobs.jsonl).",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Override: run autobid on this specific URL. Can be repeated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N URLs (default: all).",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Start from the Nth URL (1-indexed). Useful for resuming "
        "after the review prompt halted on a specific job. Default: 1.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit each application (DANGEROUS). Default is dry-run.",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=20.0,
        help="Seconds to keep each tab open after filling, for manual review "
        "(dry-run only, used only when --no-review is passed). Set 0 to "
        "close immediately. Default: 20.",
    )
    parser.add_argument(
        "--review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After each job is filled, pop a blocking Windows alert so you "
        "can manually review the form in the browser; click OK to advance "
        "to the next job. Default: ON. Use --no-review to fall back to the "
        "old timed --hold behaviour.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (default: headful so you can watch).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logs.",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    setup_logging(
        log_dir=project_root / "logs",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    try:
        cfg = load_config(project_root=project_root)
    except RuntimeError as exc:
        # The autobid CLI doesn't actually need JOBRIGHT_EMAIL/PASSWORD --
        # those are required by load_config for the main bot. Let the user
        # know but continue if we can; for now just rethrow with hint.
        logger.error(
            "Could not load config: %s. The autobid CLI shares .env with the "
            "main bot, so JOBRIGHT_EMAIL / JOBRIGHT_PASSWORD must still be set.",
            exc,
        )
        return 2

    profile = load_profile(project_root=project_root)
    if not profile.full_name or not profile.email:
        logger.error(
            "Applicant profile is incomplete: APPLICANT_FIRST_NAME / "
            "APPLICANT_LAST_NAME / APPLICANT_EMAIL must be set in .env."
        )
        return 2

    if profile.resume_path and not profile.resume_path.exists():
        logger.warning(
            "APPLICANT_RESUME_PATH=%s does not exist; resume upload will be skipped.",
            profile.resume_path,
        )

    ai = AIAnswerer.from_env(profile, project_root=project_root)

    # Build URL list.
    if args.url:
        urls = list(args.url)
        logger.info("Using %d URL(s) from --url overrides.", len(urls))
    else:
        source = Path(args.source)
        if not source.is_absolute():
            source = project_root / source
        all_urls = _read_jsonl_urls(source)
        urls = _filter_supported(all_urls)
        logger.info(
            "Loaded %d URL(s) from %s; %d are Greenhouse-supported.",
            len(all_urls),
            source,
            len(urls),
        )

    if args.start > 1:
        if args.start > len(urls):
            logger.error(
                "--start=%d is past the end of the URL list (%d).",
                args.start,
                len(urls),
            )
            return 1
        skipped = args.start - 1
        urls = urls[skipped:]
        logger.info("Skipping the first %d URL(s); starting from #%d.", skipped, args.start)

    if args.limit is not None:
        urls = urls[: args.limit]

    if not urls:
        logger.error("No URLs to process.")
        return 1

    if args.submit:
        logger.warning(
            "--submit is set: %d application(s) will be REALLY submitted!",
            len(urls),
        )

    successes = 0
    failures = 0

    with sync_playwright() as pw:
        # Use a NON-persistent context so each test run starts cleanly and we
        # don't accidentally leave session cookies in the main bot's profile.
        browser = pw.chromium.launch(
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
            proxy=cfg.proxy.to_playwright() if cfg.proxy else None,
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(cfg.action_timeout_ms)

        # When --start is used, keep the printed index aligned with the
        # original source list so the review prompt and logs show the
        # "real" job number rather than 1-of-fewer.
        total_jobs = len(urls) + (args.start - 1)
        try:
            for i, url in enumerate(urls, start=args.start):
                logger.info(
                    "=" * 60
                    + f"\n[{i}/{total_jobs}] {url}\n"
                    + "=" * 60
                )
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    result = autobid.autobid_apply(
                        page,
                        profile,
                        ai,
                        submit=args.submit,
                        action_timeout_ms=cfg.action_timeout_ms,
                    )
                    _log_result(result)
                    if result.ok:
                        successes += 1
                    else:
                        failures += 1
                    if args.submit:
                        # In --submit mode we don't pop the per-job review
                        # prompt for happy-path submissions, but if the
                        # submit failed (form errors, no change after click,
                        # verification email never arrived, ...) we DO want
                        # to grab the user's attention so they can fix the
                        # form manually before moving on.
                        if (
                            result.submission_status
                            not in ("confirmed", "verified", "not_attempted")
                            and args.review
                        ):
                            _submit_failure_alert(
                                index=i,
                                total=total_jobs,
                                url=url,
                                result=result,
                            )
                    elif result.skip_reason is not None:
                        # JD judge decided this job isn't worth bidding;
                        # nothing was filled in the form, so don't pop
                        # the review alert. Just log + advance.
                        logger.info(
                            "Skipping review prompt -- judge marked job %d/%d "
                            "as SKIP (%s).",
                            i,
                            total_jobs,
                            result.skip_reason,
                        )
                    elif args.review:
                        _review_pause(
                            index=i,
                            total=total_jobs,
                            url=url,
                            result=result,
                        )
                    elif args.hold > 0:
                        logger.info(
                            "Holding tab open for %.0fs for manual review.",
                            args.hold,
                        )
                        time.sleep(args.hold)
                except Exception:
                    logger.exception("Unhandled error processing %s", url)
                    failures += 1
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    logger.info(
        "Test run complete. urls=%d ok=%d failed=%d submit=%s",
        len(urls),
        successes,
        failures,
        args.submit,
    )
    return 0 if failures == 0 else 1


# Windows MessageBox return codes / styles. We pull them from MSDN
# rather than importing a heavyweight GUI lib -- ctypes ships with
# Python and is plenty for a single modal alert.
_MB_OK = 0x00000000
_MB_OKCANCEL = 0x00000001
_MB_ICONINFORMATION = 0x00000040
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000
_MB_SETFOREGROUND = 0x00010000
_IDOK = 1
_IDCANCEL = 2


def _submit_failure_alert(
    *,
    index: int,
    total: int,
    url: str,
    result: autobid.AutobidResult,
) -> None:
    """Pop a Windows alert when a submit attempt did NOT confirm.

    Triggered for ``form_error``, ``no_change``, and
    ``needs_code_failed`` outcomes -- the user needs to know so they
    can finish the application by hand. Always blocking; clicking OK
    advances to the next job, Cancel aborts the run.
    """

    short_url = _short_url_for_alert(url)
    title = f"Greezik SUBMIT FAILED [{index}/{total}]"
    status = result.submission_status
    if status == "needs_code_failed":
        head = "Greenhouse asked for an email verification code, but Greezik couldn't paste one back."
    elif status == "form_error":
        head = "Greenhouse rejected the submit -- visible field errors on the form."
    elif status == "no_change":
        head = "Submit click had no visible effect -- form may have failed silently."
    else:
        head = f"Submit outcome: {status}"
    detail_lines: list[str] = []
    if result.error:
        detail_lines.append(f"Error: {result.error}")
    for err in result.submission_errors[:5]:
        detail_lines.append(f" - {err}")
    detail = "\n".join(detail_lines) or "(no extra detail)"
    body = (
        f"{head}\n\n"
        f"{detail}\n\n"
        f"URL: {short_url}\n\n"
        f"Finish the application MANUALLY in the browser,\n"
        f"then click OK to move to the next job.\n"
        f"Click Cancel to abort the test run."
    )
    logger.warning("Submit failed for %s -- pausing for manual fix.", short_url)
    if sys.platform == "win32":
        try:
            import ctypes

            style = (
                _MB_OKCANCEL
                | _MB_ICONWARNING
                | _MB_TOPMOST
                | _MB_SETFOREGROUND
            )
            rc = ctypes.windll.user32.MessageBoxW(0, body, title, style)
            if rc == _IDCANCEL:
                logger.info("User aborted the test run from submit-failure prompt.")
                raise SystemExit(0)
            return
        except SystemExit:
            raise
        except Exception as exc:
            logger.warning(
                "Could not show submit-failure MessageBox (%s); "
                "falling back to console prompt.",
                exc,
            )
    print()
    print("=" * 60)
    print(f"SUBMIT FAILED [{index}/{total}] -- {head}")
    print(detail)
    print(f"URL: {short_url}")
    print("Press Enter to continue, or type 'q' + Enter to abort: ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "q"
    if ans == "q":
        logger.info("User aborted the test run from submit-failure prompt.")
        raise SystemExit(0)


def _review_pause(
    *,
    index: int,
    total: int,
    url: str,
    result: autobid.AutobidResult,
) -> None:
    """Block until the user dismisses the per-job review alert.

    On Windows we pop a real native MessageBox (so it grabs focus over
    the Chromium window). On other OSes -- or if the MessageBox call
    fails for any reason -- we fall back to a console prompt.
    """

    short_url = _short_url_for_alert(url)
    title = f"Greezik review [{index}/{total}]"
    if result.error:
        body_lead = (
            f"Job filled with ERROR: {result.error}\n"
            f"Filled {len(result.filled)} field(s); "
            f"{len(result.failed)} field(s) failed."
        )
        icon = _MB_ICONWARNING
    elif result.failed:
        body_lead = (
            f"Filled {len(result.filled)} field(s); "
            f"{len(result.failed)} field(s) FAILED -- "
            f"manual review recommended."
        )
        icon = _MB_ICONWARNING
    else:
        body_lead = (
            f"Filled {len(result.filled)} field(s); "
            f"no errors. Form is ready for review."
        )
        icon = _MB_ICONINFORMATION

    body = (
        f"{body_lead}\n\n"
        f"URL: {short_url}\n\n"
        f"Click OK in the BROWSER first to verify the fields,\n"
        f"then click OK here to move to the next job.\n"
        f"Click Cancel to abort the test run."
    )

    logger.info("Pausing for manual review of job %d/%d ...", index, total)

    if sys.platform == "win32":
        try:
            import ctypes

            style = (
                _MB_OKCANCEL
                | icon
                | _MB_TOPMOST
                | _MB_SETFOREGROUND
            )
            rc = ctypes.windll.user32.MessageBoxW(0, body, title, style)
            if rc == _IDCANCEL:
                logger.info("User aborted the test run from review prompt.")
                raise SystemExit(0)
            return
        except SystemExit:
            raise
        except Exception as exc:
            logger.warning(
                "Could not show Windows MessageBox (%s); "
                "falling back to console prompt.",
                exc,
            )

    # Non-Windows or MessageBox unavailable: console prompt.
    print()
    print("=" * 60)
    print(f"REVIEW [{index}/{total}] -- {body_lead}")
    print(f"URL: {short_url}")
    print("Press Enter to continue, or type 'q' + Enter to abort: ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "q"
    if ans == "q":
        logger.info("User aborted the test run from review prompt.")
        raise SystemExit(0)


def _short_url_for_alert(url: str) -> str:
    """Trim a Greenhouse embed URL down to the company slug for display.

    Full embed URLs are >150 chars and wrap awkwardly inside Windows
    MessageBox. The ``for=<company>`` query param is the most useful
    identifier so we surface that, then truncate.
    """

    try:
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        company = (qs.get("for") or [""])[0]
        if company:
            return f"{parsed.netloc}/...?for={company}"
    except Exception:
        pass
    if len(url) > 120:
        return url[:117] + "..."
    return url


def _log_result(result: autobid.AutobidResult) -> None:
    logger.info("Autobid result for %s:", result.url)
    logger.info("  Provider:  %s", result.provider)
    if result.jd_path is not None:
        logger.info("  JD saved:  %s", result.jd_path)
    if result.skip_reason is not None:
        logger.info("  Verdict:   SKIP (%s)", result.skip_reason)
        return
    if result.resume_used is not None:
        logger.info("  Resume:    %s", result.resume_used.name)
    logger.info("  Submitted: %s (%s)", result.submitted, result.submission_status)
    if result.submission_errors:
        for e in result.submission_errors:
            logger.warning("  Submit issue: %s", e)
    logger.info("  Filled (%d):", len(result.filled))
    for f in result.filled:
        logger.info("    + %s", f)
    if result.refilled:
        logger.info("  Refilled after clearing (%d):", len(result.refilled))
        for f in result.refilled:
            logger.info("    ~ %s", f)
    if result.skipped:
        logger.info("  Skipped (%d):", len(result.skipped))
        for s in result.skipped:
            logger.info("    - %s", s)
    if result.failed:
        logger.warning("  Failed (%d):", len(result.failed))
        for f in result.failed:
            logger.warning("    ! %s", f)
    if result.error:
        logger.error("  ERROR: %s", result.error)


if __name__ == "__main__":
    sys.exit(main())
