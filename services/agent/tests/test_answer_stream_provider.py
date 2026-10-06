"""Provider-level streaming tests for answer.compose and chat.reply."""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.llm.client import LLMStreamCancelled
from app.providers.answer import AnswerStreamCancelled, LlmAnswerProvider
from app.providers.chat import ChatReplyStreamCancelled, LlmChatReplyProvider


class FakeStreamingClient:
    def __init__(
        self,
        chunks: list[str],
        *,
        repair: str | None = None,
        raise_cancel: bool = False,
    ) -> None:
        self.chunks = chunks
        self.repair = repair
        self.raise_cancel = raise_cancel
        self.last_call_count = 0
        self.complete_calls = 0
        self.stream_calls: list[dict[str, Any]] = []

    def stream_complete(self, messages: list[dict[str, str]], **kwargs: Any):
        self.last_call_count = 1
        self.stream_calls.append({"messages": messages, **kwargs})
        if self.raise_cancel:
            raise LLMStreamCancelled("stopped")
        for chunk in self.chunks:
            yield chunk

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.complete_calls += 1
        self.last_call_count = 1
        if self.repair is None:
            raise AssertionError("repair call was not expected")
        return self.repair


def test_answer_stream_decodes_first_field_and_parses_draft() -> None:
    client = FakeStreamingClient(
        [
            '{"',
            'answer": "',
            "\\u4f60\\u597d\\u3002",
            '", "citations": []}',
        ]
    )
    provider = LlmAnswerProvider(client)
    deltas: list[str] = []

    draft = provider.stream_compose(
        "你好吗",
        [{"evidence_id": "e1", "content": "hi"}],
        on_delta=deltas.append,
        should_cancel=lambda: False,
        idle_timeout=5,
        total_timeout=10,
    )

    assert draft.answer == "你好。"
    assert draft.citations == []
    assert "".join(deltas) == "你好。"
    assert draft.model_calls == 1
    call = client.stream_calls[0]
    assert call["should_cancel"] is not None
    assert call["idle_timeout"] == 5
    assert call["total_timeout"] == 10
    assert "你好吗" in call["messages"][1]["content"]


def test_answer_stream_keeps_the_json_repair_round() -> None:
    client = FakeStreamingClient(
        ['{"answer": "broken"'],
        repair='{"answer":"fixed","citations":[]}',
    )
    provider = LlmAnswerProvider(client)
    deltas: list[str] = []

    draft = provider.stream_compose(
        "question",
        [{"evidence_id": "e1"}],
        on_delta=deltas.append,
    )

    assert draft.answer == "fixed"
    assert client.complete_calls == 1
    assert draft.model_calls == 2
    # The preview keeps the partial text; the authoritative draft is the repair.
    assert "".join(deltas) == "broken"


def test_answer_stream_cancellation_is_classified() -> None:
    provider = LlmAnswerProvider(FakeStreamingClient([], raise_cancel=True))

    with pytest.raises(AnswerStreamCancelled):
        provider.stream_compose("question", [{"evidence_id": "e1"}])


def test_chat_reply_stream_decodes_the_reply_field() -> None:
    client = FakeStreamingClient(
        ['{"', 'reply": "', "\\u665a\\u4e0a\\u597d", '"}']
    )
    provider = LlmChatReplyProvider(client)
    deltas: list[str] = []

    draft = provider.stream_reply("晚上好", on_delta=deltas.append)

    assert draft.reply == "晚上好"
    assert "".join(deltas) == "晚上好"
    assert draft.model_calls == 1


def test_chat_reply_stream_cancellation_is_classified() -> None:
    provider = LlmChatReplyProvider(FakeStreamingClient([], raise_cancel=True))

    with pytest.raises(ChatReplyStreamCancelled):
        provider.stream_reply("晚上好")


def test_non_streaming_compose_still_uses_the_same_messages() -> None:
    class CompletingClient:
        last_call_count = 0

        def complete(self, messages: list[dict[str, str]]) -> str:
            self.messages = messages
            self.last_call_count = 1
            return '{"answer":"ok","citations":[]}'

    client = CompletingClient()
    provider = LlmAnswerProvider(client)
    draft = provider.compose("question", [{"evidence_id": "e1"}])

    assert draft.answer == "ok"
    assert "question" in client.messages[1]["content"]
