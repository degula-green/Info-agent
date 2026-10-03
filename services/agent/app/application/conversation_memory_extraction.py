"""Explicit, conversation-scoped memory writes.

This is deliberately small: it records only direct statements the owner asks
the Agent to remember. It does not build a user profile or write memory visible
to another conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.application.memory_service import MemoryService
from app.config import Settings
from app.kernel.models import MemoryRecord
from app.kernel.protocols import AgentStore

USER_NAME_MEMORY_KEY = "conversation:user_name"

_STRONG_SELF_INTRO = re.compile(
    r"^(?:我叫|我的名字(?:是|叫))\s*([\w\u4e00-\u9fff·-]{1,40})"
)
_WEAK_SELF_INTRO = re.compile(r"^我是\s*([\w\u4e00-\u9fff·-]{2,20})")
_REMEMBER_COMMAND = re.compile(r"^(?:请|帮我)?记住(?:一下|住)?[，,:：\s]+(.+)$", re.S)
_NAME_REJECTIONS = (
    "一个",
    "一名",
    "负责",
    "开发",
    "产品",
    "测试",
    "运维",
    "来自",
    "做",
)


@dataclass(frozen=True)
class ExplicitMemory:
    memory_type: str
    title: str
    content: str
    memory_key: str
    keywords: tuple[str, ...]
    importance: float
    confidence: float


class ConversationMemoryExtractionService:
    """Write explicit memory statements after a Task reaches a terminal state."""

    def __init__(
        self,
        store: AgentStore,
        settings: Settings,
        memory_service: MemoryService,
    ) -> None:
        self.store = store
        self.settings = settings
        self.memory_service = memory_service

    def extract_after_task(self, task_id: str) -> list[MemoryRecord]:
        if not self.settings.conversation_memory_enabled:
            return []
        task = self.store.get_task(task_id)
        if (
            task is None
            or task.source_type != "chat"
            or not task.conversation_id
            or not task.request_message_id
        ):
            return []
        message = self.store.get_message(task.request_message_id)
        if message is None:
            return []

        records: list[MemoryRecord] = []
        for item in extract_explicit_memories(message.content):
            records.append(
                self.memory_service.upsert_conversation_memory(
                    owner_user_id=task.owner_user_id,
                    conversation_id=task.conversation_id,
                    memory_type=item.memory_type,
                    title=item.title,
                    content=item.content,
                    memory_key=item.memory_key,
                    keywords=list(item.keywords),
                    importance=item.importance,
                    confidence=item.confidence,
                    source_message_ids=[message.message_id],
                    extraction_method="explicit",
                )
            )
        return records


def extract_explicit_memories(text: str) -> list[ExplicitMemory]:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return []

    name_match = _STRONG_SELF_INTRO.match(normalized) or _WEAK_SELF_INTRO.match(
        normalized
    )
    if name_match:
        name = _clean_name(name_match.group(1))
        if name:
            return [
                ExplicitMemory(
                    memory_type="fact",
                    title="当前会话中的用户名字",
                    content=f"用户在会话中自称为“{name}”",
                    memory_key=USER_NAME_MEMORY_KEY,
                    keywords=(name, "名字", "自称", "我是"),
                    importance=0.9,
                    confidence=0.98,
                )
            ]

    remember_match = _REMEMBER_COMMAND.match(normalized)
    if remember_match:
        content = remember_match.group(1).strip()
        if content:
            return [
                ExplicitMemory(
                    memory_type="context",
                    title="用户要求记住的信息",
                    content=content,
                    memory_key=_content_memory_key(content),
                    keywords=tuple(_keywords(content)),
                    importance=0.75,
                    confidence=0.9,
                )
            ]
    return []


def _clean_name(value: str) -> str:
    name = str(value or "").strip(" \t\r\n，。,.!！?？、:：;；\"'“”‘’")
    if not name or name in {"谁", "什么", "哪位"}:
        return ""
    if any(marker in name for marker in _NAME_REJECTIONS):
        return ""
    return name[:40]


def _content_memory_key(content: str) -> str:
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"conversation:explicit:{digest[:24]}"


def _keywords(text: str) -> list[str]:
    values = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,6}", text)
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
        if len(result) >= 20:
            break
    return result


__all__ = [
    "ConversationMemoryExtractionService",
    "ExplicitMemory",
    "USER_NAME_MEMORY_KEY",
    "extract_explicit_memories",
]
