"""Redis-backed transient channel for streaming answer deltas.

PostgreSQL keeps the authoritative Task result; Redis only carries the
per-attempt preview so a reconnect can resume without replaying every delta
from the database. Every publish failure is swallowed and logged: Redis being
unavailable must degrade to "wait for task.completed", never fail the Task.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.kernel.streaming import AnswerStreamSink

logger = logging.getLogger("agent.answer_stream")

_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_MAX_LEN = 2000
_DEFAULT_SNAPSHOT_EVERY = 50


def stream_key(task_id: str, answer_id: str) -> str:
    return f"agent:task:{task_id}:answer:{answer_id}:stream"


def meta_key(task_id: str, answer_id: str) -> str:
    return f"agent:task:{task_id}:answer:{answer_id}:meta"


def snapshot_key(task_id: str, answer_id: str) -> str:
    return f"agent:task:{task_id}:answer:{answer_id}:snapshot"


def index_key(task_id: str, answer_id: str) -> str:
    return f"agent:task:{task_id}:answer:{answer_id}:seq_index"


class RedisAnswerStream:
    """Writer and reader for one task's answer deltas."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_len: int = _DEFAULT_MAX_LEN,
        snapshot_every: int = _DEFAULT_SNAPSHOT_EVERY,
    ) -> None:
        self.client = client
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_len = max(10, int(max_len))
        self.snapshot_every = max(1, int(snapshot_every))

    def open(
        self,
        *,
        task_id: str,
        answer_id: str,
        step_id: str,
        attempt: int,
    ) -> AnswerStreamSink:
        return RedisAnswerStreamSink(
            self,
            task_id=task_id,
            answer_id=answer_id,
            step_id=step_id,
            attempt=attempt,
        )

    # -- writer -----------------------------------------------------------

    def append_delta(
        self,
        *,
        task_id: str,
        answer_id: str,
        step_id: str,
        attempt: int,
        seq: int,
        offset: int,
        delta: str,
    ) -> None:
        entries = self.client.xadd(
            stream_key(task_id, answer_id),
            {
                "seq": str(seq),
                "offset": str(offset),
                "delta": delta,
                "step_id": step_id,
                "attempt": str(attempt),
            },
            maxlen=self.max_len,
            approximate=True,
        )
        entry_id = entries.decode() if isinstance(entries, bytes) else str(entries)
        now = f"{time.time():.3f}"
        pipe = self.client.pipeline()
        pipe.hset(
            meta_key(task_id, answer_id),
            mapping={
                "next_seq": str(seq),
                "offset": str(offset),
                "updated_at": now,
                "step_id": step_id,
                "attempt": str(attempt),
            },
        )
        pipe.hset(index_key(task_id, answer_id), str(seq), entry_id)
        pipe.expire(stream_key(task_id, answer_id), self.ttl_seconds)
        pipe.expire(meta_key(task_id, answer_id), self.ttl_seconds)
        pipe.expire(index_key(task_id, answer_id), self.ttl_seconds)
        pipe.execute()

    def save_snapshot(
        self, *, task_id: str, answer_id: str, text: str, next_seq: int
    ) -> None:
        payload = json.dumps(
            {"text": str(text or ""), "next_seq": int(next_seq)},
            ensure_ascii=False,
        )
        self.client.set(
            snapshot_key(task_id, answer_id),
            payload,
            ex=self.ttl_seconds,
        )

    def complete(
        self,
        *,
        task_id: str,
        answer_id: str,
        final_seq: int,
        answer: str,
        citations: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        self.save_snapshot(
            task_id=task_id,
            answer_id=answer_id,
            text=answer,
            next_seq=final_seq,
        )
        self.client.hset(
            meta_key(task_id, answer_id),
            mapping={
                "done": "1",
                "final_seq": str(int(final_seq)),
                "citations": json.dumps(list(citations or []), ensure_ascii=False),
                "citations_count": str(len(citations or [])),
                "warnings": json.dumps(list(warnings or []), ensure_ascii=False),
                "updated_at": f"{time.time():.3f}",
            },
        )
        self.client.expire(meta_key(task_id, answer_id), self.ttl_seconds)

    def interrupt(self, *, task_id: str, answer_id: str, reason: str) -> None:
        self.client.hset(
            meta_key(task_id, answer_id),
            mapping={
                "interrupted": "1",
                "reason": str(reason or "interrupted"),
                "updated_at": f"{time.time():.3f}",
            },
        )
        self.client.expire(meta_key(task_id, answer_id), self.ttl_seconds)

    # -- reader -----------------------------------------------------------

    def read_deltas(
        self,
        *,
        task_id: str,
        answer_id: str,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        start = "-"
        index = self.client.hget(index_key(task_id, answer_id), str(int(after_seq)))
        if index:
            index_id = index.decode() if isinstance(index, bytes) else str(index)
            start = f"({index_id}"
        entries = self.client.xrange(
            stream_key(task_id, answer_id),
            min=start,
            max="+",
            count=max(1, int(limit)),
        )
        result: list[dict[str, Any]] = []
        for entry_id, fields in entries or []:
            decoded = _decode_fields(fields)
            try:
                seq = int(decoded.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= int(after_seq):
                continue
            result.append(
                {
                    "stream_id": (
                        entry_id.decode()
                        if isinstance(entry_id, bytes)
                        else str(entry_id)
                    ),
                    "seq": seq,
                    "offset": _int_or_zero(decoded.get("offset")),
                    "delta": str(decoded.get("delta") or ""),
                    "step_id": str(decoded.get("step_id") or ""),
                    "attempt": _int_or_zero(decoded.get("attempt")),
                }
            )
        return result

    def status(self, *, task_id: str, answer_id: str) -> dict[str, Any]:
        raw = self.client.hgetall(meta_key(task_id, answer_id)) or {}
        meta = _decode_fields(raw)
        return {
            "answer_id": answer_id,
            "next_seq": _int_or_zero(meta.get("next_seq")),
            "final_seq": _int_or_zero(meta.get("final_seq")),
            "done": str(meta.get("done") or "") == "1",
            "interrupted": str(meta.get("interrupted") or "") == "1",
            "reason": str(meta.get("reason") or ""),
        }

    def snapshot(self, *, task_id: str, answer_id: str) -> dict[str, Any] | None:
        raw = self.client.get(snapshot_key(task_id, answer_id))
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        state = self.status(task_id=task_id, answer_id=answer_id)
        meta = _decode_fields(
            self.client.hgetall(meta_key(task_id, answer_id)) or {}
        )
        return {
            "answer_id": answer_id,
            "text": str(data.get("text") or ""),
            "next_seq": _int_or_zero(data.get("next_seq")),
            "completed": bool(state.get("done")),
            "final_seq": _int_or_zero(state.get("final_seq")),
            "interrupted": bool(state.get("interrupted")),
            "citations": _json_list(meta.get("citations")),
            "warnings": _json_list(meta.get("warnings")),
        }


class RedisAnswerStreamSink:
    """Accumulates one attempt and mirrors it into Redis without failing it."""

    def __init__(
        self,
        stream: RedisAnswerStream,
        *,
        task_id: str,
        answer_id: str,
        step_id: str,
        attempt: int,
    ) -> None:
        self._stream = stream
        self._task_id = task_id
        self.answer_id = answer_id
        self.step_id = step_id
        self.attempt = int(attempt)
        self.final_seq = 0
        self.offset = 0
        self.answer = ""
        self.citations: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.completed = False
        self.interrupted = False

    def push(self, delta: str) -> None:
        if self.completed or self.interrupted or not delta:
            return
        self.final_seq += 1
        self.offset += len(delta.encode("utf-8"))
        self.answer += delta
        try:
            self._stream.append_delta(
                task_id=self._task_id,
                answer_id=self.answer_id,
                step_id=self.step_id,
                attempt=self.attempt,
                seq=self.final_seq,
                offset=self.offset,
                delta=delta,
            )
            if self.final_seq % self._stream.snapshot_every == 0:
                self._stream.save_snapshot(
                    task_id=self._task_id,
                    answer_id=self.answer_id,
                    text=self.answer,
                    next_seq=self.final_seq,
                )
        except Exception:  # noqa: BLE001 - Redis must not fail the answer
            logger.exception(
                "answer delta publish failed task=%s answer=%s seq=%s",
                self._task_id,
                self.answer_id,
                self.final_seq,
            )

    def complete(
        self,
        *,
        answer: str,
        citations: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        self.answer = str(answer or "")
        self.citations = list(citations or [])
        self.warnings = list(warnings or [])
        self.completed = True
        try:
            self._stream.complete(
                task_id=self._task_id,
                answer_id=self.answer_id,
                final_seq=self.final_seq,
                answer=self.answer,
                citations=self.citations,
                warnings=self.warnings,
            )
        except Exception:  # noqa: BLE001 - Redis must not fail the answer
            logger.exception(
                "answer completion publish failed task=%s answer=%s",
                self._task_id,
                self.answer_id,
            )

    def interrupt(self, reason: str) -> None:
        self.interrupted = True
        try:
            self._stream.interrupt(
                task_id=self._task_id,
                answer_id=self.answer_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - Redis must not fail the answer
            logger.exception(
                "answer interrupt publish failed task=%s answer=%s",
                self._task_id,
                self.answer_id,
            )


def _decode_fields(fields: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (fields or {}).items():
        name = key.decode() if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            result[name] = value.decode("utf-8", errors="replace")
        else:
            result[name] = str(value)
    return result


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
