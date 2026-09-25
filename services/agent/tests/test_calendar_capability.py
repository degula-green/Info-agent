"""calendar.create capability: validation, time resolution and error mapping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.capabilities.calendar import CalendarCreateCapability
from app.infrastructure.knowledge.client import (
    CalendarAuthorizationInvalid,
    CalendarNotBound,
    CalendarRejected,
    CalendarResultUnknown,
)
from app.kernel.errors import PermanentCapabilityError, UnknownExternalResultError
from app.testing.fake_knowledge import FakeKnowledgeClient

REQUEST_ID = "knowledge_event:user-1:item-1:3"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def capability(knowledge: FakeKnowledgeClient | None = None, **overrides) -> CalendarCreateCapability:
    return CalendarCreateCapability(knowledge or FakeKnowledgeClient(), **overrides)


def arguments(**overrides):
    base = {
        "title": "评审会",
        "time_expression": "明天晚上八点",
        "timezone": "Asia/Shanghai",
        "owner_user_id": "user-1",
        "idempotency_key": REQUEST_ID,
        "source": {"sent_at": iso(datetime.now(timezone.utc) - timedelta(hours=1))},
    }
    base.update(overrides)
    return base


def test_validate_rejects_a_step_without_any_time_input() -> None:
    with pytest.raises(ValidationError):
        capability().validate(arguments(time_expression=None, start_time=None))


def test_validate_rejects_an_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        capability().validate(arguments(timezone="Mars/Olympus"))


def test_validate_rejects_malformed_or_reversed_explicit_times() -> None:
    with pytest.raises(ValidationError):
        capability().validate(arguments(start_time="tomorrow 8pm"))
    with pytest.raises(ValidationError):
        capability().validate(
            arguments(
                start_time="2026-09-26T20:00:00+08:00",
                end_time="2026-09-26T19:00:00+08:00",
            )
        )


def test_execute_asks_for_the_time_when_the_expression_has_no_clock() -> None:
    result = capability().execute(capability().validate(arguments(time_expression="明天")))

    assert result == {
        "requires_user_input": True,
        "missing_information": ["start_time"],
        "reason": "start_time_unresolved",
    }


def test_execute_does_not_create_a_schedule_in_the_past() -> None:
    stale = iso(datetime.now(timezone.utc) - timedelta(days=10))
    payload = arguments(source={"sent_at": stale})

    result = capability().execute(capability().validate(payload))

    assert result["requires_user_input"] is True
    assert result["missing_information"] == ["start_time"]
    assert result["reason"] == "start_time_in_the_past"


def test_execute_calls_knowledge_with_the_business_request_id() -> None:
    knowledge = FakeKnowledgeClient()
    instance = capability(knowledge)
    payload = arguments(location="会议室 A")

    result = instance.execute(instance.validate(payload))

    assert len(knowledge.calendar_calls) == 1
    call = knowledge.calendar_calls[0]
    assert call["request_id"] == REQUEST_ID
    assert call["owner_user_id"] == "user-1"
    assert call["provider"] == "feishu"
    assert call["title"] == "评审会"
    assert call["timezone"] == "Asia/Shanghai"
    assert call["location"] == "会议室 A"
    assert call["source"] == payload["source"]
    assert result["request_id"] == REQUEST_ID
    start = datetime.fromisoformat(call["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(call["end_time"].replace("Z", "+00:00"))
    assert start.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M") == "20:00"
    assert start > datetime.now(timezone.utc)
    assert end - start == timedelta(minutes=60)
    assert result["event_id"] == "evt-1"
    assert result["operation"] == "calendar.create"


def test_execute_uses_a_shorter_default_duration_when_configured() -> None:
    knowledge = FakeKnowledgeClient()
    instance = capability(knowledge, default_duration_minutes=30)

    instance.execute(instance.validate(arguments()))

    call = knowledge.calendar_calls[0]
    start = datetime.fromisoformat(call["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(call["end_time"].replace("Z", "+00:00"))
    assert end - start == timedelta(minutes=30)


def test_execute_uses_the_edited_time_from_the_approval() -> None:
    knowledge = FakeKnowledgeClient()
    instance = capability(knowledge)
    edited = arguments(start_time="2026-09-27T09:30:00+08:00", time_expression=None)

    instance.execute(instance.validate(edited))

    call = knowledge.calendar_calls[0]
    assert call["start_time"] == "2026-09-27T01:30:00Z"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CalendarNotBound("no calendar"), "calendar_not_bound"),
        (CalendarAuthorizationInvalid("renew"), "calendar_authorization_invalid"),
    ],
)
def test_execute_waits_for_input_when_the_calendar_is_not_usable(error, reason) -> None:
    knowledge = FakeKnowledgeClient(calendar_error=error)
    instance = capability(knowledge)

    result = instance.execute(instance.validate(arguments()))

    assert result == {
        "requires_user_input": True,
        "missing_information": ["calendar_authorization"],
        "reason": reason,
    }


def test_execute_maps_permanent_and_unknown_calendar_failures() -> None:
    rejected = FakeKnowledgeClient(calendar_error=CalendarRejected("bad request"))
    with pytest.raises(PermanentCapabilityError):
        rejected_instance = capability(rejected)
        rejected_instance.execute(rejected_instance.validate(arguments()))

    unknown = FakeKnowledgeClient(calendar_error=CalendarResultUnknown("timeout"))
    with pytest.raises(UnknownExternalResultError):
        unknown_instance = capability(unknown)
        unknown_instance.execute(unknown_instance.validate(arguments()))
