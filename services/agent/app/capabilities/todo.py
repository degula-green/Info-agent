"""todo.create: the to-do ledger the Agent owns itself.

A to-do is what the owner must still act on: a meeting, an invitation, a
reminder or a piece of work. It is deliberately *not* one kind of thing -- the
product decision is that a schedule and a to-do are the same object, so there
is no second intent and no second capability.

Two properties differ from a calendar event and shape this file:

* Time is optional. "完成登录模块代码" has no time at all, and "下周三下午找个时间"
  has one that cannot be resolved. Neither may block creating the to-do.
* The row outlives the Task. An overdue to-do keeps showing on the desktop
  until the owner deletes it, so nothing here expires or auto-completes it.

The write is local to this service: there is no Knowledge or calendar call
behind it. Authorization and the vendor live client-side, which is also why
the capability no longer needs a Knowledge client at all.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.timeparse import resolve_time_expression
from app.kernel.events import utcnow
from app.kernel.models import CapabilityDescriptor, TodoRecord

CAPABILITY_NAME = "todo.create"

# The Agent default; the owner's desktop may override it when editing.
DEFAULT_TIMEZONE = "Asia/Shanghai"

logger = logging.getLogger("agent.capability.todo")


class TodoSource(BaseModel):
    """Provenance carried from the collected message; used for tracing only."""

    model_config = ConfigDict(extra="forbid")

    knowledge_item_id: str | None = None
    content_version: int | None = None
    conversation_type: str | None = None
    sender_display_name: str | None = None
    sent_at: str | None = None


class TodoCreateInput(BaseModel):
    """Validated arguments of todo.create.

    Nothing here is required except the title: a to-do without a time is the
    normal case, not a missing-input case.
    """

    # An unknown field means the caller invented one (the LLM planner did:
    # todo.create(time=...)). Dropping it silently would lose the value and the
    # mistake would only surface once the step executed.
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_expression: str | None = None
    due_at: str | None = None
    timezone: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    owner_user_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    source: TodoSource = Field(default_factory=TodoSource)

    @model_validator(mode="after")
    def _check(self) -> "TodoCreateInput":
        if self.due_at is not None and _parse_iso(self.due_at) is None:
            raise ValueError("due_at must be an ISO 8601 timestamp with a timezone")
        return self


class TodoCreateResult(BaseModel):
    """What the Planner may assume about a todo.create step output."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    request_id: str
    status: str
    todo_id: str
    title: str
    due_at: str | None = None
    due_expression: str | None = None
    todo_status: str


def _parse_iso(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def load_timezone(name: str | None, fallback: str = DEFAULT_TIMEZONE) -> ZoneInfo | None:
    """Returns a zone when the name is usable, None when it is not.

    An unknown zone must not fail the write: the to-do still exists, only the
    resolved timestamp is dropped in favour of the raw phrase.
    """

    candidate = (name or fallback or "").strip()
    if not candidate:
        return None
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve_due(
    due_expression: str | None,
    *,
    base: datetime,
    zone: ZoneInfo,
) -> datetime | None:
    """Resolves the phrase best effort: an ambiguous one yields None.

    This mirrors the calendar parser and never guesses. Unlike the calendar
    version the None is not a problem to fix later -- it is stored as the
    "we do not know when yet" state the owner can edit on the desktop.
    """

    return resolve_time_expression(due_expression or "", base, zone)


class TodoCreateCapability:
    """Writes one to-do row; never plans, never decides approvals."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description="创建一条待办：会议、邀约、提醒、杂事都算；时间可选",
        input_schema=TodoCreateInput.model_json_schema(),
        output_schema=TodoCreateResult.model_json_schema(),
        # The write lands on a surface the owner relies on, so it is treated
        # like any other user-visible write: preview, then approve.
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        # The outcome is readable: ``execute`` looks the row up by idempotency
        # key before writing, so a retry can never create a second to-do and
        # nobody has to guess whether the write landed.
        reconcilable=True,
        timeout_seconds=10,
    )

    def __init__(self, store, *, default_timezone: str = DEFAULT_TIMEZONE) -> None:
        self.store = store
        self.default_timezone = default_timezone

    def validate(self, arguments: dict[str, Any]) -> TodoCreateInput:
        return TodoCreateInput.model_validate(arguments)

    def execute(self, arguments: TodoCreateInput) -> dict[str, Any]:
        existing = self.store.find_todo_by_idempotency_key(arguments.idempotency_key)
        if existing is not None:
            # A retried Step must not create a second row.
            return self._result(existing, status="already_exists")

        zone = load_timezone(arguments.timezone, self.default_timezone)
        due_at = _parse_iso(arguments.due_at)
        if due_at is None and zone is not None:
            base = _parse_iso(arguments.source.sent_at) or utcnow()
            due_at = resolve_due(arguments.due_expression, base=base, zone=zone)
        if due_at is None and arguments.due_expression:
            # Keep the phrase even when it cannot be resolved, so the desktop
            # still shows "下周三下午" instead of silently losing it.
            logger.info(
                "todo %s keeps an unresolved due expression: %s",
                arguments.idempotency_key,
                arguments.due_expression,
            )

        moment = utcnow()
        todo = TodoRecord(
            todo_id=str(uuid4()),
            owner_user_id=arguments.owner_user_id,
            title=arguments.title,
            due_at=due_at,
            due_expression=(arguments.due_expression or None),
            timezone=str(zone) if zone is not None else None,
            notes=arguments.notes,
            status="open",
            source=arguments.source.model_dump(exclude_none=True),
            idempotency_key=arguments.idempotency_key,
            created_at=moment,
            updated_at=moment,
        )
        saved = self.store.create_todo(todo)
        return self._result(saved, status="created")

    @staticmethod
    def _result(todo: TodoRecord, *, status: str) -> dict[str, Any]:
        return {
            "operation": CAPABILITY_NAME,
            "request_id": todo.idempotency_key,
            "status": status,
            "todo_id": todo.todo_id,
            "title": todo.title,
            "due_at": (
                todo.due_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if todo.due_at
                else None
            ),
            "due_expression": todo.due_expression,
            "todo_status": todo.status,
        }
