"""In-memory Knowledge double used by the step-2 tests (and stage 2a)."""

from __future__ import annotations

from typing import Any, Mapping

from app.infrastructure.knowledge.client import KnowledgeItemNotFound


class FakeKnowledgeClient:
    def __init__(
        self,
        *,
        snapshots: Mapping[str, Mapping[str, Any]] | None = None,
        snapshot_error: Exception | None = None,
        calendar_error: Exception | None = None,
        calendar_result: Mapping[str, Any] | None = None,
    ) -> None:
        self.snapshots = dict(snapshots or {})
        self.snapshot_error = snapshot_error
        self.calendar_error = calendar_error
        self.calendar_result = dict(calendar_result or {})
        self.snapshot_calls: list[str] = []
        self.calendar_calls: list[dict[str, Any]] = []

    def conversation_snapshot(self, knowledge_item_id: str) -> dict[str, Any]:
        self.snapshot_calls.append(knowledge_item_id)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        snapshot = self.snapshots.get(knowledge_item_id)
        if snapshot is None:
            raise KnowledgeItemNotFound(f"unknown knowledge item: {knowledge_item_id}")
        return dict(snapshot)

    def create_calendar_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        call = dict(payload)
        self.calendar_calls.append(call)
        if self.calendar_error is not None:
            raise self.calendar_error
        result = {
            "request_id": call.get("request_id"),
            "provider": call.get("provider", "feishu"),
            "event_id": f"evt-{len(self.calendar_calls)}",
            "event_url": f"https://calendar.example/{call.get('request_id')}",
            "status": "created",
        }
        result.update(self.calendar_result)
        return result
