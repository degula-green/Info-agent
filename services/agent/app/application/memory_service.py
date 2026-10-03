"""Conversation-scoped structured memory management."""

from __future__ import annotations

import hashlib
import re
from uuid import uuid4

from app.kernel.errors import AgentContractError
from app.kernel.events import utcnow
from app.kernel.models import MemoryRecord
from app.kernel.protocols import AgentStore

ALLOWED_MEMORY_TYPES = frozenset({"fact", "decision", "relation", "context"})


class MemoryNotFoundError(AgentContractError):
    pass


class MemoryPermissionError(AgentContractError):
    pass


class MemoryValidationError(AgentContractError):
    pass


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _memory_key(title: str, content: str) -> str:
    digest = hashlib.sha256(
        f"{_normalize_text(title)}\n{_normalize_text(content)}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


class MemoryService:
    def __init__(self, store: AgentStore) -> None:
        self.store = store

    def _owned_conversation(self, *, conversation_id: str, owner_user_id: str):
        conversation = self.store.get_conversation(conversation_id)
        if conversation is None:
            raise MemoryNotFoundError("conversation not found")
        if conversation.owner_user_id != owner_user_id:
            raise MemoryPermissionError(
                "conversation does not belong to the current user"
            )
        return conversation

    def create(
        self,
        *,
        owner_user_id: str,
        conversation_id: str,
        memory_type: str,
        title: str,
        content: str,
        keywords: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 0.8,
    ) -> MemoryRecord:
        conversation = self._owned_conversation(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        resolved_type = str(memory_type or "").strip().lower()
        if resolved_type not in ALLOWED_MEMORY_TYPES:
            raise MemoryValidationError(
                "memory_type must be one of fact, decision, relation, context"
            )
        resolved_title = _normalize_text(title)
        resolved_content = _normalize_text(content)
        if not resolved_title:
            raise MemoryValidationError("title is required")
        if not resolved_content:
            raise MemoryValidationError("content is required")
        moment = utcnow()
        memory = MemoryRecord(
            memory_id=str(uuid4()),
            owner_user_id=owner_user_id,
            organization_id=conversation.organization_id,
            memory_type=resolved_type,
            scope="conversation",
            title=resolved_title,
            content=resolved_content,
            content_hash=hashlib.sha256(resolved_content.encode("utf-8")).hexdigest(),
            memory_key=_memory_key(resolved_title, resolved_content),
            keywords=[
                item.strip()
                for item in (keywords or [])
                if item and item.strip()
            ][:20],
            source_conversation_id=conversation_id,
            extraction_method="manual",
            confidence=max(0.0, min(1.0, float(confidence))),
            importance=max(0.0, min(1.0, float(importance))),
            status="active",
            created_at=moment,
            updated_at=moment,
        )
        return self.store.create_memory(memory)

    def list(
        self,
        *,
        owner_user_id: str,
        conversation_id: str,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        self._owned_conversation(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        types = None
        if memory_type:
            resolved_type = str(memory_type).strip().lower()
            if resolved_type not in ALLOWED_MEMORY_TYPES:
                raise MemoryValidationError("unsupported memory_type")
            types = [resolved_type]
        return self.store.list_memories(
            owner_user_id,
            conversation_id=conversation_id,
            statuses=["active"],
            memory_types=types,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        *,
        owner_user_id: str,
        conversation_id: str,
        memory_type: str | None = None,
    ) -> int:
        self._owned_conversation(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        types = [memory_type] if memory_type else None
        return self.store.count_memories(
            owner_user_id,
            conversation_id=conversation_id,
            statuses=["active"],
            memory_types=types,
        )

    def delete(
        self,
        *,
        owner_user_id: str,
        conversation_id: str,
        memory_id: str,
    ) -> None:
        self._owned_conversation(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        if not self.store.delete_memory(
            memory_id,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
        ):
            raise MemoryNotFoundError("memory not found")

    def retrieve_for_context(
        self,
        *,
        owner_user_id: str,
        conversation_id: str,
        query: str,
        limit: int,
    ) -> list[MemoryRecord]:
        return self.store.search_memories(
            owner_user_id,
            conversation_id=conversation_id,
            query=query,
            limit=limit,
        )
