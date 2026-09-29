"""In-memory Knowledge double used by the step-2 tests (and stage 2a)."""

from __future__ import annotations

from typing import Any, Mapping

from app.infrastructure.knowledge.client import KnowledgeItemNotFound


class FakeKnowledgeClient:
    """Only the snapshot: the to-do ledger lives in the Agent, not in Knowledge."""

    def __init__(
        self,
        *,
        snapshots: Mapping[str, Mapping[str, Any]] | None = None,
        snapshot_error: Exception | None = None,
    ) -> None:
        self.snapshots = dict(snapshots or {})
        self.snapshot_error = snapshot_error
        self.snapshot_calls: list[str] = []

    def conversation_snapshot(self, knowledge_item_id: str) -> dict[str, Any]:
        self.snapshot_calls.append(knowledge_item_id)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        snapshot = self.snapshots.get(knowledge_item_id)
        if snapshot is None:
            raise KnowledgeItemNotFound(f"unknown knowledge item: {knowledge_item_id}")
        return dict(snapshot)
