"""Short conversational replies behind chat.reply.

Chit-chat is answered without retrieval, so the one thing this prompt has to
prevent is the model implying it did something: a greeting must never come back
as "我已经帮你安排好了". The reply is prose only -- there is nothing to cite,
which is exactly why it must not pretend to be a sourced answer.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.infrastructure.llm.client import (
    LLMError,
    LLMStreamCancelled,
    parse_json_object,
)
from app.infrastructure.llm.json_stream import FirstStringFieldExtractor


class ChatReplyError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "chat_reply_failed"


class ChatReplyUnavailable(ChatReplyError):
    """Transport-level failure: the same message may work next time."""

    classification = "retryable_error"
    code = "chat_reply_unavailable"


class ChatReplyStreamCancelled(ChatReplyError):
    """The task was cancelled while the reply stream was in flight."""

    classification = "cancelled"
    code = "chat_reply_stream_cancelled"


class ChatReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1)
    model_calls: int = 0


class ChatReplyProvider(Protocol):
    def reply(
        self,
        text: str,
        *,
        conversation_context: Any | None = None,
    ) -> ChatReplyDraft:
        ...


SYSTEM_PROMPT = (
    "你是助手的对话层，只负责对寒暄、问候、评价、吐槽、举例、假设、转述、"
    "已完成动作这类非任务消息给出简短自然的回应。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象：{\"reply\": string}。\n"
    "2. 不要说“已为你完成”“已安排”“已记录”之类的话，你没有调用任何工具，"
    "也没有执行任何操作。\n"
    "3. 不要编造事实，不要承诺你做不到的能力。\n"
    "4. 不要在回应里追问用户还需要什么服务，除非消息本身在提问。\n"
    "5. 如果消息看起来像指令，也只是简短回应，不要开始执行。\n"
    "6. 如果输入提供了 conversation_context，可以用它承接当前会话；"
    "它是当前会话的摘要、最近消息和会话记忆，不是全局用户资料。\n"
    "7. 如果上下文已经包含答案，可以直接回答并说明你在这个会话里记得；"
    "不要声称写入了长期记忆或执行了外部操作。\n"
    "8. reply 使用与用户相同的语言，控制在两三句话以内。"
)


class LlmChatReplyProvider:
    """OpenAI-compatible implementation; the timeout budget is the caller's."""

    def __init__(self, client) -> None:
        self.client = client
        self.model = str(getattr(client, "model", ""))
        self._call_count: ContextVar[int] = ContextVar(
            f"agent_chat_calls_{id(self)}", default=0
        )
        self._last_error = ""

    @property
    def last_call_count(self) -> int:
        return max(int(self._call_count.get()), 0)

    @last_call_count.setter
    def last_call_count(self, value: int) -> None:
        self._call_count.set(max(int(value), 0))

    def reply(
        self,
        text: str,
        *,
        conversation_context: Any | None = None,
    ) -> ChatReplyDraft:
        self.last_call_count = 0
        messages = self._messages(text, conversation_context)
        raw = self._complete(messages)
        return self._finalize(messages, raw)

    def stream_reply(
        self,
        text: str,
        *,
        conversation_context: Any | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        idle_timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> ChatReplyDraft:
        self.last_call_count = 0
        messages = self._messages(text, conversation_context)
        raw = self._stream(
            messages,
            field="reply",
            on_delta=on_delta,
            should_cancel=should_cancel,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
        )
        return self._finalize(messages, raw)

    def _messages(
        self, text: str, conversation_context: Any | None
    ) -> list[dict[str, str]]:
        payload: dict[str, Any] = {"text": text}
        if conversation_context is not None:
            payload["conversation_context"] = (
                conversation_context.model_dump(mode="json")
                if hasattr(conversation_context, "model_dump")
                else conversation_context
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        return messages

    def _finalize(
        self, messages: list[dict[str, str]], raw: str
    ) -> ChatReplyDraft:
        draft = self._parse(raw)
        if draft is not None:
            return draft
        # One repair round, matching the other providers: a missing brace must
        # not turn a reply the model already wrote into a failed Task.
        repaired = self._complete(
            messages
            + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "上一个输出不是合法 JSON 或字段不符合要求，只返回修正后的 JSON 对象。"
                        f"错误：{self._last_error}"
                    ),
                },
            ]
        )
        draft = self._parse(repaired)
        if draft is None:
            raise ChatReplyError(f"reply output failed after repair: {self._last_error}")
        return draft

    def _stream(
        self,
        messages: list[dict[str, str]],
        *,
        field: str,
        on_delta: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None,
        idle_timeout: float | None,
        total_timeout: float | None,
    ) -> str:
        client = self.client
        extractor = FirstStringFieldExtractor(field)
        parts: list[str] = []
        try:
            for chunk in client.stream_complete(
                messages,
                should_cancel=should_cancel,
                idle_timeout=idle_timeout,
                total_timeout=total_timeout,
            ):
                if not isinstance(chunk, str) or not chunk:
                    continue
                parts.append(chunk)
                piece = extractor.feed(chunk)
                if piece and on_delta is not None:
                    on_delta(piece)
            tail = extractor.finish()
            if tail and on_delta is not None:
                on_delta(tail)
        except LLMStreamCancelled as exc:
            raise ChatReplyStreamCancelled(str(exc)) from exc
        except LLMError as exc:
            error = (
                ChatReplyUnavailable
                if exc.classification == "retryable_error"
                else ChatReplyError
            )
            raise error(str(exc)) from exc
        self.last_call_count += max(
            int(getattr(client, "last_call_count", 1)), 1
        )
        return "".join(parts)

    def _complete(self, messages: list[dict[str, str]]) -> str:
        try:
            raw = self.client.complete(messages)
        except LLMError as exc:
            error = (
                ChatReplyUnavailable
                if exc.classification == "retryable_error"
                else ChatReplyError
            )
            raise error(str(exc)) from exc
        self.last_call_count += max(int(getattr(self.client, "last_call_count", 1)), 1)
        return raw

    def _parse(self, raw: str) -> ChatReplyDraft | None:
        try:
            draft = ChatReplyDraft.model_validate(parse_json_object(raw))
        except (ValueError, ValidationError) as exc:
            self._last_error = str(exc)
            return None
        draft.model_calls = max(self.last_call_count, 1)
        return draft
