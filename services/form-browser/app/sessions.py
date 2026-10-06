"""Browser session lifecycle.

One task gets one session so the page keeps its login state, scroll position
and selection between the preview step and the write step. Sessions expire on
their own: a leaked Chromium process is the failure mode worth designing out.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings
from .grid import GridDriver


class SessionNotFound(KeyError):
    pass


class TooManySessions(RuntimeError):
    pass


@dataclass
class Session:
    session_id: str
    context: Any
    page: Any
    settings: Settings
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    url: str = ""
    login_required: bool = False

    @property
    def grid(self) -> GridDriver:
        return GridDriver(self.page, settle_ms=self.settings.settle_ms)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def expired(self, ttl_seconds: int) -> bool:
        return (time.monotonic() - self.last_used) > max(ttl_seconds, 1)


class SessionManager:
    """Owns the Playwright instance and every live context."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless
        )

    async def stop(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def reap(self) -> None:
        """Close sessions that nobody touched within the TTL."""

        async with self._lock:
            stale = [
                session_id
                for session_id, session in self._sessions.items()
                if session.expired(self.settings.session_ttl_seconds)
            ]
        for session_id in stale:
            await self.close(session_id)

    async def create(self) -> Session:
        await self.reap()
        async with self._lock:
            if len(self._sessions) >= max(self.settings.max_sessions, 1):
                raise TooManySessions("session limit reached")
        if self._browser is None:
            raise RuntimeError("browser is not started")

        options: dict[str, Any] = {
            "locale": self.settings.locale,
            "viewport": {"width": 1440, "height": 900},
            "user_agent": self.settings.default_user_agent,
        }
        if self.settings.storage_state_path and Path(
            self.settings.storage_state_path
        ).exists():
            options["storage_state"] = self.settings.storage_state_path
        context = await self._browser.new_context(**options)
        # The grid driver reads selected cells back through the clipboard.
        try:
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:  # noqa: BLE001 - not fatal, reads just fail loudly
            pass
        page = await context.new_page()
        session = Session(
            session_id=str(uuid4()),
            context=context,
            page=page,
            settings=self.settings,
        )
        async with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> Session:
        await self.reap()
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        session.touch()
        return session

    async def close(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            if not session.login_required:
                await self.persist_state(session)
            await session.context.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    async def persist_state(self, session: Session) -> None:
        """Save the live login cookies/local storage for later sessions."""

        path = str(self.settings.storage_state_path or "").strip()
        if not path:
            return
        try:
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            await session.context.storage_state(path=str(target))
        except Exception:  # noqa: BLE001 - persistence must not break the action
            return

    def count(self) -> int:
        return len(self._sessions)
