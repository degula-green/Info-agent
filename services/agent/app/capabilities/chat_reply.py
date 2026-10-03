"""chat.reply: a short reply for messages that are not tasks.

The Runtime has no notion of a conversation, so a greeting is answered the same
way every other outcome is: one Step, one Observation, one answer. It is
deliberately separate from ``answer.compose`` -- that one exists to organise
evidence and refuses to run without sources, which is the wrong shape for
"晚上好".
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.kernel.execution_context import current_execution_context
from app.kernel.models import CapabilityDescriptor

CAPABILITY_NAME = "chat.reply"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TEXT_CHARS = 4000


class ChatReplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ChatReplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Named ``answer`` on purpose: the Runtime treats any successful Step whose
    # output carries an answer as the finished work, so the front end renders a
    # greeting exactly like any other reply.
    answer: str
    model_calls: int


class ChatReplyCapability:
    """Calls the chat provider; read-only, no evidence, no side effects."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description=(
            "对寒暄、问候、评价、吐槽、举例、假设、转述、已完成动作等非任务消息"
            "给出简短自然的回复（只读）。它不检索、不引用证据，因此需要依据资料"
            "回答的问题必须用 answer.compose，不要用本能力。"
        ),
        input_schema=ChatReplyInput.model_json_schema(),
        output_schema=ChatReplyResult.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    def __init__(self, provider, *, timeout_seconds: int | None = None) -> None:
        self.provider = provider
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> ChatReplyInput:
        return ChatReplyInput.model_validate(arguments)

    def execute(self, arguments: ChatReplyInput) -> dict[str, Any]:
        draft = _reply_with_context(self.provider, arguments.text)
        return ChatReplyResult(
            answer=draft.reply,
            # Reported so the Runtime can charge the Task budget: a capability
            # that spends model calls must not be able to spend them for free.
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()


def _reply_with_context(provider, text: str):
    try:
        context = current_execution_context().conversation_context
    except RuntimeError:
        context = None
    if context is None:
        return provider.reply(text)

    signature = inspect.signature(provider.reply)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if "conversation_context" not in signature.parameters and not accepts_kwargs:
        return provider.reply(text)
    return provider.reply(text, conversation_context=context)
