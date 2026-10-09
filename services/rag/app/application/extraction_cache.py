"""Reuse extraction results and keep the call rate under a ceiling.

Why a cache is not optional here: ``_run_windows`` aborts the whole conversation
on the first failed window and deliberately leaves the watermark where it was,
so the next sweep re-extracts **every** window of that conversation. Without a
cache the windows that already succeeded are paid for again, and the model is
the expensive part of the sweep (measured 1.3-6.5s and hundreds of tokens per
window).

The prompt is the cache key because it fully determines the answer for a fixed
model and parameter set: same window text and same prompt template means the
same extraction. Only successful calls are stored - an exception propagates and
is never cached, otherwise a transient provider failure would be replayed as if
it were the model's answer for that window.

Both helpers are shared across the scan's worker threads, hence the locks.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping


def _digest(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


class ExtractionCache:
    """Bounded LRU over parsed extraction payloads."""

    def __init__(self, *, max_entries: int = 512) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, prompt: str) -> dict[str, Any] | None:
        key = _digest(prompt)
        with self._lock:
            payload = self._entries.get(key)
            if payload is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            # Copied out so a caller cannot mutate the cached entry.
            return copy.deepcopy(payload)

    def put(self, prompt: str, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            return
        key = _digest(prompt)
        with self._lock:
            self._entries[key] = copy.deepcopy(dict(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._entries),
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


class CallRateLimiter:
    """Space out call starts so the aggregate rate stays under a ceiling.

    Concurrency already bounds how many calls are in flight; this bounds how
    many *start* per second, which is the shape most provider quotas take. A
    zero interval means no limiting, so the default costs nothing.
    """

    def __init__(self, *, min_interval_seconds: float = 0.0) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_slot)
            self._next_slot = start_at + self.min_interval_seconds
        delay = start_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
