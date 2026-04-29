"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    post_load_wait_seconds: float
    redirect_wait_seconds: float
    applied_urls_file: Path
    browser_profile_dir: Path
    action_timeout_ms: int

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

    return Config(
        email=_get_str("JOBRIGHT_EMAIL", required=True),
        password=_get_str("JOBRIGHT_PASSWORD", required=True),
        post_load_wait_seconds=_get_float("POST_LOAD_WAIT_SECONDS", 3.0),
        redirect_wait_seconds=_get_float("REDIRECT_WAIT_SECONDS", 15.0),
        applied_urls_file=applied_file,
        browser_profile_dir=profile_dir,
        action_timeout_ms=_get_int("ACTION_TIMEOUT_MS", 15_000),
    )
