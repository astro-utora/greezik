"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyConfig:
    """Playwright-shaped proxy settings parsed from ``BROWSER_PROXY``."""

    server: str
    username: str = ""
    password: str = ""

    def to_playwright(self) -> dict[str, str]:
        """Return a dict suitable for ``chromium.launch(proxy=...)``."""
        out: dict[str, str] = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    post_load_wait_seconds: float
    redirect_wait_seconds: float
    applied_urls_file: Path
    skipped_urls_file: Path
    browser_profile_dir: Path
    action_timeout_ms: int
    proxy: ProxyConfig | None = None
    # Per-company dedup window. If we've already applied to this
    # company within ``company_dedup_days`` days, the new opening is
    # logged to ``skipped_jobs.jsonl`` and not re-bid. Set to 0 to
    # disable.
    company_dedup_days: int = 3
    # When True, the live runner actually clicks "Submit application"
    # on Greenhouse forms. When False, the form is filled but left
    # open for manual review.
    submit_greenhouse: bool = True

    @property
    def action_timeout_seconds(self) -> float:
        return self.action_timeout_ms / 1000.0


def _get_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        if required:
            raise RuntimeError(
                f"Missing required environment variable {name!r}. "
                "Copy .env.example to .env and fill it in."
            )
        return default if default is not None else ""
    return value


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number, got {raw!r}") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise RuntimeError(
        f"Environment variable {name} must be a boolean (true/false), got {raw!r}"
    )


def _parse_proxy(raw: str) -> ProxyConfig | None:
    """Parse ``BROWSER_PROXY`` into a :class:`ProxyConfig`.

    Accepts every reasonable shape we've seen in proxy lists:

    * ``host:port``
    * ``host:port:user:pass``           (common bullet-list format)
    * ``user:pass@host:port``           (URL-style auth)
    * ``http://host:port`` / ``http://user:pass@host:port`` / ``socks5://...``

    Returns ``None`` for an empty / blank value so callers can ignore it.
    """

    raw = (raw or "").strip()
    if not raw:
        return None

    # Already a URL? Use urlsplit.
    if "://" in raw:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or ""
        if not host or not parts.port:
            raise RuntimeError(f"BROWSER_PROXY {raw!r} is missing host or port.")
        server = f"{scheme}://{host}:{parts.port}"
        return ProxyConfig(
            server=server,
            username=parts.username or "",
            password=parts.password or "",
        )

    # user:pass@host:port (URL-style without scheme).
    if "@" in raw:
        creds, _, hostpart = raw.rpartition("@")
        user, _, pwd = creds.partition(":")
        host, _, port = hostpart.partition(":")
        if not host or not port:
            raise RuntimeError(f"BROWSER_PROXY {raw!r} is missing host or port.")
        return ProxyConfig(
            server=f"http://{host}:{port}",
            username=user,
            password=pwd,
        )

    # Bullet-list format: host:port[:user:pass]
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        return ProxyConfig(server=f"http://{host}:{port}")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return ProxyConfig(server=f"http://{host}:{port}", username=user, password=pwd)

    raise RuntimeError(
        f"BROWSER_PROXY {raw!r} is not in a recognised format. "
        "Use host:port, host:port:user:pass, user:pass@host:port, "
        "or a full URL like http://user:pass@host:port."
    )


def load_config(project_root: Path | None = None) -> Config:
    """Load configuration from .env (if present) and the process environment."""

    root = (project_root or Path.cwd()).resolve()
    load_dotenv(dotenv_path=root / ".env", override=False)

    profile_dir = Path(_get_str("BROWSER_PROFILE_DIR", ".pw_profile"))
    if not profile_dir.is_absolute():
        profile_dir = root / profile_dir

    applied_file = Path(_get_str("APPLIED_URLS_FILE", "applied_jobs.jsonl"))
    if not applied_file.is_absolute():
        applied_file = root / applied_file

    skipped_file = Path(_get_str("SKIPPED_URLS_FILE", "skipped_jobs.jsonl"))
    if not skipped_file.is_absolute():
        skipped_file = root / skipped_file

    proxy = _parse_proxy(_get_str("BROWSER_PROXY", ""))
    if proxy is not None:
        # Don't log credentials.
        logger.info("Using browser proxy %s (auth=%s)", proxy.server, "yes" if proxy.username else "no")

    return Config(
        email=_get_str("JOBRIGHT_EMAIL", required=True),
        password=_get_str("JOBRIGHT_PASSWORD", required=True),
        post_load_wait_seconds=_get_float("POST_LOAD_WAIT_SECONDS", 3.0),
        redirect_wait_seconds=_get_float("REDIRECT_WAIT_SECONDS", 30.0),
        applied_urls_file=applied_file,
        skipped_urls_file=skipped_file,
        browser_profile_dir=profile_dir,
        action_timeout_ms=_get_int("ACTION_TIMEOUT_MS", 15_000),
        proxy=proxy,
        company_dedup_days=_get_int("COMPANY_DEDUP_DAYS", 3),
        submit_greenhouse=_get_bool("SUBMIT_GREENHOUSE", True),
    )
