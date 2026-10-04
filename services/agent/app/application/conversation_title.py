"""Automatic conversation titles.

The title is user-visible history metadata, not a memory summary. It is
generated once after the first completed turn and written back to the existing
``conversation.title`` field. Manual renames are never overwritten.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app.config import Settings
from app.infrastructure.llm.client import parse_json_object
from app.kernel.errors import AgentContractError
from app.kernel.events import utcnow
from app.kernel.models import ConversationRecord, MessageRecord
from app.kernel.protocols import AgentStore

logger = logging.getLogger("agent.conversation_title")

DEFAULT_TITLES = {"", "新的对话"}


def fallback_title(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return "新的对话"
    return normalized if len(normalized) <= 200 else normalized[:197] + "..."


class ConversationTitleProvider(Protocol):
    def generate(self, messages: list[MessageRecord], *, max_chars: int) -> str:
        ...


class LlmConversationTitleProvider:
    """Generate a short title from the first user/assistant exchange."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def generate(self, messages: list[MessageRecord], *, max_chars: int) -> str:
        rendered = "\n".join(
            f"{item.role}: {item.content}" for item in messages if item.content
        )
        prompt = (
            "根据首轮会话生成一个简短标题。只输出 JSON："
            '{"title":"..."}。\n'
            f"要求：中文优先；不超过 {max_chars} 个字符；"
            "概括用户真正想解决的问题；不要加引号、句号、Markdown 或解释。\n\n"
            f"会话内容：\n{rendered}\n"
        )
        raw = self.client.complete(
            [
                {"role": "system", "content": "你是会话标题生成器，只返回合法 JSON。"},
                {"role": "user", "content": prompt},
            ]
        )
        try:
            body = parse_json_object(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentContractError("title model returned invalid JSON") from exc
        title = str(body.get("title") or "").strip() if isinstance(body, dict) else ""
        if not title:
            raise AgentContractError("title model returned an empty title")
        return normalize_title(title, max_chars=max_chars)


def normalize_title(value: str, *, max_chars: int) -> str:
    title = " ".join(str(value or "").split()).strip(" \t\r\n\"'“”‘’`")
    title = title.rstrip("。.!！?？")
    if not title:
        return ""
    limit = max(1, int(max_chars))
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


class ConversationTitleService:
    def __init__(
        self,
        store: AgentStore,
        settings: Settings,
        provider: ConversationTitleProvider,
    ) -> None:
        self.store = store
        self.settings = settings
        self.provider = provider

    def maybe_generate_after_task(self, task_id: str) -> str | None:
        if not self.settings.conversation_title_enabled:
            return None
        task = self.store.get_task(task_id)
        if (
            task is None
            or not task.conversation_id
        ):
            return None
        conversation = self.store.get_conversation(task.conversation_id)
        if conversation is None or conversation.owner_user_id != task.owner_user_id:
            return None
        messages = self.store.list_messages(task.conversation_id)
        first_turn = _first_turn(messages)
        if first_turn is None or not _title_is_default(conversation, first_turn):
            return None

        title = self.provider.generate(
            first_turn,
            max_chars=max(1, self.settings.conversation_title_max_chars),
        )
        title = normalize_title(title, max_chars=self.settings.conversation_title_max_chars)
        if not title:
            return None

        refreshed = self.store.get_conversation(task.conversation_id)
        if refreshed is None or not _title_is_default(refreshed, first_turn):
            return None
        refreshed.title = title
        refreshed.updated_at = utcnow()
        self.store.save_conversation(refreshed)
        logger.info(
            "generated title for conversation %s from task %s",
            task.conversation_id,
            task.task_id,
        )
        return title


def _first_turn(messages: list[MessageRecord]) -> list[MessageRecord] | None:
    user = next(
        (
            item
            for item in messages
            if item.role == "user" and item.status == "completed" and item.content.strip()
        ),
        None,
    )
    if user is None:
        return None
    assistant = next(
        (
            item
            for item in messages
            if item.role == "assistant"
            and item.status == "completed"
            and item.content.strip()
            and item.created_at >= user.created_at
        ),
        None,
    )
    return [user, assistant] if assistant is not None else [user]


def _title_is_default(
    conversation: ConversationRecord,
    first_turn: list[MessageRecord],
) -> bool:
    title = conversation.title.strip()
    if title in DEFAULT_TITLES:
        return True
    first_user = first_turn[0].content
    return title == fallback_title(first_user)
