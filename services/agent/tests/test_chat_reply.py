"""chat.reply: the conversational answer for messages that are not tasks.

Two things matter here: the reply lands in the shape the Runtime already knows
how to surface (an ``answer`` in the step output), and a model failure keeps its
classification so the kernel can tell "try again" from "give up".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.capabilities.chat_reply import (
    ChatReplyCapability,
    ChatReplyInput,
    ChatReplyResult,
)
from app.infrastructure.llm.client import LLMError, LLMUnavailable
from app.providers.chat import ChatReplyDraft, ChatReplyError, LlmChatReplyProvider


class StubClient:
    """Returns scripted raw answers; records the messages it was given."""

    model = "stub-chat"

    def __init__(self, outputs: list[str], *, error: Exception | None = None) -> None:
        self.outputs = list(outputs)
        self.error = error
        self.calls: list[list[dict[str, str]]] = []
        self.last_call_count = 1

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0)


class FakeProvider:
    def __init__(self, reply: str = "晚上好呀", *, model_calls: int = 1) -> None:
        self.text = reply
        self.model_calls = model_calls
        self.calls: list[str] = []

    def reply(self, text: str) -> ChatReplyDraft:
        self.calls.append(text)
        return ChatReplyDraft(reply=self.text, model_calls=self.model_calls)


def test_the_capability_reports_the_reply_as_an_answer() -> None:
    """The output key is what makes the front end render it as a reply."""

    provider = FakeProvider("晚上好，需要我做什么随时说", model_calls=2)
    capability = ChatReplyCapability(provider)

    result = capability.execute(capability.validate({"text": "晚上好"}))

    assert result["answer"] == "晚上好，需要我做什么随时说"
    assert result["model_calls"] == 2
    assert provider.calls == ["晚上好"]


def test_an_empty_reply_is_refused() -> None:
    capability = ChatReplyCapability(FakeProvider())

    with pytest.raises(ValidationError):
        capability.validate({"text": ""})


def test_unknown_arguments_are_rejected() -> None:
    capability = ChatReplyCapability(FakeProvider())

    with pytest.raises(ValidationError):
        capability.validate({"text": "晚上好", "evidence": []})


def test_the_descriptor_is_read_only_and_needs_no_approval() -> None:
    descriptor = ChatReplyCapability(FakeProvider()).descriptor

    assert descriptor.name == "chat.reply"
    assert descriptor.side_effect is False
    assert descriptor.requires_approval is False
    assert descriptor.input_schema == ChatReplyInput.model_json_schema()
    assert descriptor.output_schema == ChatReplyResult.model_json_schema()


def test_the_provider_parses_the_reply_and_counts_its_calls() -> None:
    client = StubClient(['{"reply": "哈哈，确实"}'])
    provider = LlmChatReplyProvider(client)

    draft = provider.reply("今天这个会开得挺久的")

    assert draft.reply == "哈哈，确实"
    assert draft.model_calls == 1


def test_the_provider_repairs_invalid_json_once() -> None:
    client = StubClient(["not json at all", '{"reply": "修好了"}'])
    provider = LlmChatReplyProvider(client)

    draft = provider.reply("晚上好")

    assert draft.reply == "修好了"
    assert len(client.calls) == 2
    assert "只返回修正后的 JSON 对象" in client.calls[1][-1]["content"]


def test_the_provider_fails_after_one_repair() -> None:
    client = StubClient(["still not json", "nor this"])
    provider = LlmChatReplyProvider(client)

    with pytest.raises(ChatReplyError):
        provider.reply("晚上好")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMUnavailable("timeout"), ChatReplyError),
        (LLMError("bad request"), ChatReplyError),
    ],
)
def test_a_model_failure_keeps_its_classification(error, expected) -> None:
    """A retryable transport failure must not look like a permanent one."""

    client = StubClient([], error=error)
    provider = LlmChatReplyProvider(client)

    with pytest.raises(expected) as excinfo:
        provider.reply("晚上好")

    classification = excinfo.value.classification
    assert classification == (
        "retryable_error" if isinstance(error, LLMUnavailable) else "permanent_error"
    )
