from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator

from app.kernel.models import ConversationContext, Plan, PlanStep, TaskRecord


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    plan_id: str
    step_id: str
    owner_user_id: str
    organization_id: str | None
    request_id: str
    trace_id: str
    source_type: str
    source_ref: dict[str, Any]
    conversation_context: ConversationContext | None = None

    @classmethod
    def from_task(
        cls,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep,
        *,
        request_id: str,
        conversation_context: ConversationContext | None = None,
    ) -> "ExecutionContext":
        source_ref = dict(task.source_ref)
        organization_id = _text(source_ref.get("organization_id"))
        trace_id = (
            _text(source_ref.get("trace_id"))
            or _text(task.constraints.get("trace_id"))
            or request_id
        )
        return cls(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            owner_user_id=task.owner_user_id,
            organization_id=organization_id,
            request_id=request_id,
            trace_id=trace_id,
            source_type=task.source_type,
            source_ref=source_ref,
            conversation_context=conversation_context,
        )


_execution_context: ContextVar[ExecutionContext | None] = ContextVar(
    "agent_execution_context",
    default=None,
)


def current_execution_context() -> ExecutionContext:
    context = _execution_context.get()
    if context is None:
        raise RuntimeError("capability execution context is not available")
    return context


@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]:
    token: Token[ExecutionContext | None] = _execution_context.set(context)
    try:
        yield
    finally:
        _execution_context.reset(token)


_conversation_context: ContextVar[ConversationContext | None] = ContextVar(
    "agent_conversation_context",
    default=None,
)


def current_conversation_context() -> ConversationContext | None:
    return _conversation_context.get()


@contextmanager
def bind_conversation_context(
    context: ConversationContext | None,
) -> Iterator[None]:
    token = _conversation_context.set(context)
    try:
        yield
    finally:
        _conversation_context.reset(token)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
