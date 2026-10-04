"""Settings for the form-browser service."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _text(default: str, *names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _int(default: int, *names: str) -> int:
    for name in names:
        value = os.environ.get(name)
        if value is None or not value.strip():
            continue
        try:
            return int(value.strip())
        except ValueError:
            continue
    return default


def _bool(default: bool, *names: str) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value is None or not value.strip():
            continue
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


@dataclass(frozen=True)
class Settings:
    """Runtime configuration; env-driven so the container is portable."""

    host: str = _text("0.0.0.0", "FORM_BROWSER_HOST")
    port: int = _int(8500, "FORM_BROWSER_PORT")
    # Mirrors the Crawl4AI sidecar: every endpoint needs the shared token.
    api_token: str = _text("", "FORM_BROWSER_API_TOKEN")
    headless: bool = _bool(True, "FORM_BROWSER_HEADLESS")
    # Where a logged-in storage state is kept between sessions.
    storage_state_path: str = _text(
        "./data/form-browser-state.json", "FORM_BROWSER_STORAGE_STATE"
    )
    # A session that nobody touches is closed instead of leaking Chromium.
    session_ttl_seconds: int = _int(900, "FORM_BROWSER_SESSION_TTL_SECONDS")
    max_sessions: int = _int(4, "FORM_BROWSER_MAX_SESSIONS")
    nav_timeout_seconds: int = _int(60, "FORM_BROWSER_NAV_TIMEOUT_SECONDS")
    settle_ms: int = _int(8000, "FORM_BROWSER_SETTLE_MS")
    # Spreadsheet grids render to canvas, so rows are read through the
    # select-all/clipboard path rather than the DOM.
    max_grid_rows: int = _int(200, "FORM_BROWSER_MAX_GRID_ROWS")
    default_user_agent: str = _text(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "FORM_BROWSER_USER_AGENT",
    )
    locale: str = _text("zh-CN", "FORM_BROWSER_LOCALE")


def load_settings() -> Settings:
    return Settings()
