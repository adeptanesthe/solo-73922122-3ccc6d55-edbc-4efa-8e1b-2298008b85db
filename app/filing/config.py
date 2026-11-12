"""Configuration for the filing subsystem.

The portal URL and the demonstration account are fixed by configuration
(environment variables with safe local defaults pointing at the bundled
local portal); callers of the HTTP API cannot override them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _default_db_path() -> str:
    """Pick a writable SQLite path.

    ``/data/filing.db`` is used inside the container (a writable mounted volume
    owned by the app user).  Outside the container, where ``/data`` is usually
    not writable, fall back to a local file in the current working directory.
    """

    explicit = os.getenv("FILING_DB_PATH")
    if explicit:
        return explicit
    container_path = "/data"
    if os.path.isdir(container_path) and os.access(container_path, os.W_OK):
        return "/data/filing.db"
    return os.path.join(os.getcwd(), "filing.db")


@dataclass(frozen=True)
class Settings:
    """Runtime settings, sourced once from environment variables."""

    portal_base_url: str = field(
        default_factory=lambda: _env("PORTAL_BASE_URL", "http://portal:8010").rstrip("/")
    )
    portal_username: str = field(
        default_factory=lambda: _env("PORTAL_USERNAME", "demo")
    )
    portal_password: str = field(
        default_factory=lambda: _env("PORTAL_PASSWORD", "demo-pass")
    )
    database_path: str = field(
        default_factory=lambda: _default_db_path()
    )
    headless: bool = field(
        default_factory=lambda: _env("PLAYWRIGHT_HEADLESS", "1")
        not in ("0", "false", "False")
    )
    # Per-step browser timeout and retry budget for flaky / cut connections.
    browser_timeout_ms: int = field(
        default_factory=lambda: int(_env("BROWSER_TIMEOUT_MS", "20000"))
    )
    step_max_attempts: int = field(
        default_factory=lambda: int(_env("STEP_MAX_ATTEMPTS", "4"))
    )
    # Lines that the bundled local portal pre-populates as an existing draft.
    seed_line_ids: tuple[str, ...] = field(
        default_factory=lambda: _split_csv(os.getenv("PORTAL_SEED_LINES"))
        or ("L-SEED-01", "L-SEED-02")
    )

    def __post_init__(self) -> None:
        if not self.portal_base_url.startswith(("http://", "https://")):
            raise ValueError("PORTAL_BASE_URL must start with http:// or https://")
        if not self.portal_username:
            raise ValueError("PORTAL_USERNAME must not be empty")


_cached: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached settings instance."""

    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached
