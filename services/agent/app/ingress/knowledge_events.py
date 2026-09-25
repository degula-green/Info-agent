"""Deterministic fan-out for ``knowledge.ready`` calendar candidates.

Visibility is decided by Knowledge: it returns the owners that are both
identity-mapped and still active in the conversation. This module only applies
the platform/type pre-filter and turns one collected message into one
TaskEnvelope per eligible owner.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.models import TaskEnvelope

DEFAULT_PLATFORMS: tuple[str, ...] = ("feishu", "wecom", "wechat")

# The v1 pre-filter is intentionally small and deterministic: it only lowers the
# number of useless Tasks and never replaces task understanding. Refining the
# vocabulary must not move this check out of the ingress.
SCHEDULE_KEYWORDS: tuple[str, ...] = (
    "会议",
    "开会",
    "日程",
    "安排",
    "预约",
    "碰一下",
    "碰头",
    "评审",
    "汇报",
    "讨论",
    "聚餐",
    "面试",
    "培训",
    "分享",
    "例会",
    "面谈",
)

TIME_EXPRESSION_PATTERN = re.compile(
    r"(\d{1,2}\s*[:：]\s*\d{2})"  # 8:30
    r"|(\d{1,2}\s*[点时]\s*(\d{1,2}\s*分?|半)?)"  # 8点 / 8点半 / 8时30分
    r"|(\d{1,2}\s*月\s*\d{1,2}\s*[日号])"  # 3月5日
    r"|(今天|明天|后天|大后天|今晚|今早|明晚|本周|下周|下下周|这周|下个月|本月)"
    r"|(周[一二三四五六日天])"
    r"|(星期[一二三四五六日天])"
    r"|(礼拜[一二三四五六日天])"
    r"|(上午|中午|下午|晚上|早上|傍晚|凌晨)"
)


class KnowledgeEventIngress:
    """Turns a collected message plus its Knowledge snapshot into Tasks."""

    def __init__(self, platforms: Iterable[str] | None = None) -> None:
        resolved = DEFAULT_PLATFORMS if platforms is None else platforms
        self.platforms = frozenset(
            str(item).strip().lower() for item in resolved if str(item).strip()
        )

    def is_candidate(
        self,
        event: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
        *,
        text: str | None = None,
    ) -> bool:
        """Structural pre-filter; no external identity is compared here."""

        if str(event.get("event_type") or "") != "knowledge.ready":
            return False
        fields = self._fields(event, snapshot)
        if not self._text(fields.get("source_message_id")):
            return False
        if str(fields.get("platform") or "").strip().lower() not in self.platforms:
            return False
        message_type = fields.get("message_type", fields.get("type"))
        if str(message_type or "").strip().lower() != "text":
            return False
        return self.schedule_hint(self.resolve_text(event, snapshot, text=text))

    def create_tasks(
        self,
        event: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
        *,
        text: str | None = None,
        created_at: datetime | None = None,
        task_id_factory: Callable[[], str] | None = None,
    ) -> list[TaskEnvelope]:
        """Creates one TaskEnvelope per eligible owner; a private chat yields one."""

        moment = created_at or datetime.now(timezone.utc)
        if not self.is_candidate(event, snapshot, text=text):
            return []
        fields = self._fields(event, snapshot)
        message_id = str(fields["source_message_id"])
        body = self.resolve_text(event, snapshot, text=text)
        source_ref = {
            key: value
            for key, value in {
                "event_id": event.get("event_id"),
                "source_message_id": message_id,
                "platform": fields.get("platform"),
                "conversation_ingestion_id": fields.get("conversation_ingestion_id"),
                "conversation_type": fields.get("conversation_type"),
                "content_version": fields.get("content_version"),
                "acl_version": fields.get("acl_version"),
            }.items()
            if value is not None
        }
        new_id = task_id_factory or (lambda: str(uuid4()))
        return [
            TaskEnvelope(
                task_id=new_id(),
                source_type="knowledge_event",
                owner_user_id=owner_user_id,
                input={"source_message_id": message_id, "text": body},
                source_ref=dict(source_ref),
                created_at=moment,
            )
            for owner_user_id in self.eligible_owners(snapshot)
        ]

    @staticmethod
    def eligible_owners(snapshot: Mapping[str, Any] | None) -> list[str]:
        """Owners Knowledge already proved identity-mapped and still active."""

        if not isinstance(snapshot, Mapping):
            return []
        owners: list[str] = []
        for entry in snapshot.get("eligible_owners") or []:
            value = entry.get("owner_user_id") if isinstance(entry, Mapping) else entry
            resolved = str(value or "").strip()
            if resolved and resolved not in owners:
                owners.append(resolved)
        return owners

    @staticmethod
    def schedule_hint(text: str | None) -> bool:
        """Deterministic hint: a time expression or a schedule keyword."""

        if not text:
            return False
        if TIME_EXPRESSION_PATTERN.search(text):
            return True
        return any(keyword in text for keyword in SCHEDULE_KEYWORDS)

    @staticmethod
    def idempotency_key(message_id: str, content_version: Any, owner_user_id: str) -> str:
        """One Task per (message version, owner); matches the step-2 spec."""

        return f"knowledge_event:{owner_user_id}:{message_id}:{content_version}"

    @staticmethod
    def resolve_text(
        event: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
        *,
        text: str | None = None,
    ) -> str | None:
        if text and str(text).strip():
            return str(text)
        fields = KnowledgeEventIngress._fields(event, snapshot)
        for key in ("text", "body", "content"):
            value = fields.get(key)
            if value is not None and str(value).strip():
                return str(value)
        return None

    @staticmethod
    def _fields(
        event: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """Event payload wins over the snapshot; both carry knowledge-owned data."""

        fields: dict[str, Any] = {}
        if isinstance(snapshot, Mapping):
            fields.update({key: value for key, value in snapshot.items() if value is not None})
        payload = event.get("payload")
        source = payload if isinstance(payload, Mapping) else event
        fields.update({key: value for key, value in source.items() if value is not None})
        return fields

    @staticmethod
    def _text(value: Any) -> bool:
        return bool(value is not None and str(value).strip())
