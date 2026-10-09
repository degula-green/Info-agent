"""The window scan runs on its own interval, behind the ingestion lanes."""

import contextlib
import threading
import time

from app.application.runtime import MVPWorkerRuntime
from app.config import settings
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


@contextlib.contextmanager
def _settings(**overrides):
    """Settings is a frozen dataclass; tests override it the same way the
    existing retrieval tests do, and always restore it."""
    original = {key: getattr(settings, key) for key in overrides}
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    try:
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(settings, key, value)


class FakeScanService:
    def __init__(self, fail: bool = False):
        self.calls: list[int] = []
        self.fail = fail

    def run_once(self, *, conversation_limit: int):
        self.calls.append(conversation_limit)
        if self.fail:
            raise RuntimeError("scan blew up")


class BlockingScanService:
    """Stays inside run_once until released, to prove the caller is not blocked."""

    def __init__(self):
        self.started = 0
        self._release = threading.Event()

    def run_once(self, *, conversation_limit: int):
        self.started += 1
        self._release.wait(timeout=5)

    def release(self):
        self._release.set()


def _drain(runtime):
    thread = runtime._window_scan_thread
    if thread is not None:
        thread.join(timeout=5)


def _runtime(service):
    return MVPWorkerRuntime(
        repository=InMemoryRagMVPRepository(),
        parse_service=object(),
        index_service=object(),
        memory_service=object(),
        callback_lane=object(),
        window_scan_service=service,
    )


def test_scan_waits_for_its_interval():
    with _settings(window_scan_enabled=True, window_scan_interval_seconds=300,
                   window_scan_conversation_limit=3):
        service = FakeScanService()
        runtime = _runtime(service)

        runtime._maybe_scan_windows()
        _drain(runtime)
        runtime._maybe_scan_windows()
        _drain(runtime)

        # The second tick is inside the interval, so the scan must not run
        # again: otherwise a 0.5s poll loop would hammer the model endpoint.
        assert service.calls == [3]

        runtime._last_window_scan -= 301
        runtime._maybe_scan_windows()
        _drain(runtime)

        assert service.calls == [3, 3]


def test_scan_is_skipped_when_disabled():
    with _settings(window_scan_enabled=False):
        service = FakeScanService()
        _runtime(service)._maybe_scan_windows()
        assert service.calls == []


def test_scan_is_skipped_when_not_wired():
    with _settings(window_scan_enabled=True):
        # Must not raise when no worker was configured.
        _runtime(None)._maybe_scan_windows()


def test_scan_failure_does_not_escape_into_the_loop():
    with _settings(window_scan_enabled=True, window_scan_interval_seconds=300):
        service = FakeScanService(fail=True)
        runtime = _runtime(service)

        # The lane loop must survive a failing sweep and try again next interval.
        runtime._maybe_scan_windows()
        _drain(runtime)
        assert len(service.calls) == 1

        runtime._last_window_scan -= 301
        runtime._maybe_scan_windows()
        _drain(runtime)
        assert len(service.calls) == 2


def test_slow_scan_does_not_block_the_caller_and_never_overlaps():
    with _settings(window_scan_enabled=True, window_scan_interval_seconds=300,
                   window_scan_conversation_limit=3):
        service = BlockingScanService()
        runtime = _runtime(service)

        started = time.monotonic()
        runtime._maybe_scan_windows()
        elapsed = time.monotonic() - started

        # A sweep can take minutes; returning late here would stall the
        # callback lane with it.
        assert elapsed < 0.5
        assert runtime._window_scan_thread is not None
        assert service.started == 1

        # Even past the interval, a second sweep must not start while one is
        # still running, or the model endpoint gets two concurrent sweeps.
        runtime._last_window_scan -= 301
        runtime._maybe_scan_windows()
        assert service.started == 1

        service.release()
        _drain(runtime)
