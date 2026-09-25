"""``calendar.create``: the first real external capability.

The capability owns everything calendar specific (time parsing, timezone
normalisation, default duration, authorization signalling) and stays behind the
kernel's plain ``validate`` / ``execute`` protocol. It never plans and never
decides approvals.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.infrastructure.knowledge.client import (
    CalendarAuthorizationInvalid,
    CalendarNotBound,
    CalendarRejected,
    CalendarResultUnknown,
    KnowledgeClient,
    KnowledgeForbidden,
)
from app.ingress.vocabulary import NUMERAL, TIME_PHRASE_PATTERN
from app.kernel.errors import PermanentCapabilityError, UnknownExternalResultError
from app.kernel.models import CapabilityDescriptor

CAPABILITY_NAME = "calendar.create"
DEFAULT_PROVIDER = "feishu"

logger = logging.getLogger("agent.capability.calendar")

_NORMALISE = str.maketrans("０１２３４５６７８９：", "0123456789:")

_DAY_WORDS: tuple[tuple[str, int], ...] = (
    ("大后天", 3),
    ("今天", 0),
    ("今日", 0),
    ("明天", 1),
    ("明日", 1),
    ("后天", 2),
)

_EVENING_WORDS: tuple[tuple[str, int, str], ...] = (
    ("今晚", 0, "晚上"),
    ("明晚", 1, "晚上"),
    ("今早", 0, "早上"),
)

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# Period prefix -> (earliest valid hour, latest valid hour, add 12 when hour < 12)
_PERIODS: dict[str, tuple[int, int, bool]] = {
    "凌晨": (0, 5, False),
    "早上": (5, 11, False),
    "上午": (6, 11, False),
    "中午": (11, 13, False),
    "下午": (0, 11, True),
    "傍晚": (0, 11, True),
    "晚上": (0, 11, True),
}

_DATE_PATTERN = re.compile(r"(?P<day>\d{1,2})\s*月\s*(?P<month_day>\d{1,2})\s*[日号]")
_WEEK_PATTERN = re.compile(r"(?P<prefix>下下|下|这|本)?\s*(?:周|星期|礼拜)\s*(?P<weekday>[一二三四五六日天])")
_CLOCK_PATTERN = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    rf"(?P<hour>{NUMERAL})\s*"
    rf"(?:[:：]\s*(?P<minute>\d{{2}})|点\s*(?:(?P<half>半)|(?P<minute_cn>{NUMERAL})\s*分?)?)"
)


class CalendarSource(BaseModel):
    """Provenance carried from the collected message; only used for tracing."""

    model_config = ConfigDict(extra="ignore")

    knowledge_item_id: str | None = None
    content_version: int | None = None
    conversation_type: str | None = None
    sent_at: str | None = None


class CalendarCreateInput(BaseModel):
    """Validated arguments of ``calendar.create``."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    time_expression: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner_user_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    provider: str = DEFAULT_PROVIDER
    source: CalendarSource = Field(default_factory=CalendarSource)

    @model_validator(mode="after")
    def _check(self) -> "CalendarCreateInput":
        # The field must be present: an empty ``time_expression`` is a legitimate
        # "we know it is schedule related but not when" and is resolved by asking
        # the user instead of failing the plan.
        if self.time_expression is None and self.start_time is None:
            raise ValueError("time_expression or start_time is required")
        for field_name in ("start_time", "end_time"):
            value = getattr(self, field_name)
            if value is not None and _parse_iso(value) is None:
                raise ValueError(f"{field_name} must be an ISO 8601 timestamp")
        start, end = _parse_iso(self.start_time or ""), _parse_iso(self.end_time or "")
        if start is not None and end is not None and end <= start:
            raise ValueError("end_time must be after start_time")
        return self


def _parse_iso(value: str) -> datetime | None:
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


def load_timezone(name: str | None, fallback: str) -> ZoneInfo:
    candidate = (name or fallback or "").strip()
    if not candidate:
        raise ValueError("timezone is required")
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {candidate}") from exc


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(value: str | None) -> int | None:
    """Parses ``8``, ``十二`` or ``四十五``; returns None when it is not a number."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


def _extract_date(text: str, base_date: date) -> tuple[date | None, bool]:
    for word, offset in _DAY_WORDS:
        if word in text:
            return base_date + timedelta(days=offset), True
    for word, offset, _period in _EVENING_WORDS:
        if word in text:
            return base_date + timedelta(days=offset), True
    match = _DATE_PATTERN.search(text)
    if match:
        month, day = int(match.group("day")), int(match.group("month_day"))
        try:
            candidate = date(base_date.year, month, day)
        except ValueError:
            return None, True
        if candidate < base_date:
            try:
                candidate = date(base_date.year + 1, month, day)
            except ValueError:
                return None, True
        return candidate, True
    match = _WEEK_PATTERN.search(text)
    if match:
        weekday = _WEEKDAYS[match.group("weekday")]
        week_start = base_date - timedelta(days=base_date.weekday())
        prefix = match.group("prefix")
        if prefix == "下":
            week_start += timedelta(days=7)
        elif prefix == "下下":
            week_start += timedelta(days=14)
        candidate = week_start + timedelta(days=weekday)
        if prefix is None and candidate < base_date:
            candidate += timedelta(days=7)
        return candidate, True
    return None, False


def _extract_clock(text: str) -> tuple[time | None, bool]:
    # 今晚 / 明晚 / 今早 carry the period themselves.
    normalized = text.replace("今晚", "晚上").replace("明晚", "晚上").replace("今早", "早上")
    match = _CLOCK_PATTERN.search(normalized)
    if not match:
        return None, False
    hour = _to_int(match.group("hour"))
    if hour is None:
        return None, True
    colon_form = match.group("minute") is not None
    if colon_form:
        minute = int(match.group("minute"))
    elif match.group("half"):
        minute = 30
    elif match.group("minute_cn"):
        minute = _to_int(match.group("minute_cn"))
        if minute is None:
            return None, True
    else:
        minute = 0
    if minute > 59:
        return None, True
    period = match.group("period")
    if period:
        lowest, highest, shift = _PERIODS[period]
        if not lowest <= hour <= highest:
            return None, True
        if shift:
            hour += 12
    elif colon_form:
        # "8:30" is an explicit 24h clock reading, not a bare "八点".
        if hour > 23:
            return None, True
    elif hour >= 13 or hour == 12:
        pass
    else:
        # "八点" without 上午/下午 is ambiguous; never guess.
        return None, True
    return time(hour=hour, minute=minute), True


def parse_time_expression(expression: str | None, base: datetime, tz: ZoneInfo) -> datetime | None:
    """Resolves the v1 subset of natural-language times, or returns ``None``."""

    text = str(expression or "").translate(_NORMALISE)
    if not text.strip():
        return None
    local_base = base.astimezone(tz)
    target_date, has_date = _extract_date(text, local_base.date())
    clock, has_clock = _extract_clock(text)
    if not has_clock or clock is None:
        return None
    if target_date is None:
        if has_date:
            return None
        target_date = local_base.date()
    return datetime(
        target_date.year, target_date.month, target_date.day, clock.hour, clock.minute, tzinfo=tz
    )


def extract_time_expression(text: str) -> tuple[str, str | None]:
    """Returns ``(phrase, matched)`` where phrase may be empty.

    The phrase is the maximal date + period + clock run, so "明天晚上八点" is kept
    whole instead of the bare "明天" a loose pre-filter would stop at.
    """

    match = TIME_PHRASE_PATTERN.search(str(text or ""))
    if match is None:
        return "", None
    phrase = match.group(0).strip()
    return phrase, match.group(0)


class CalendarCreateCapability:
    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description="创建日历事件",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        timeout_seconds=20,
    )

    def __init__(
        self,
        knowledge: KnowledgeClient,
        *,
        default_timezone: str = "Asia/Shanghai",
        default_duration_minutes: int = 60,
    ) -> None:
        self.knowledge = knowledge
        self.default_timezone = default_timezone
        self.default_duration = timedelta(minutes=max(1, int(default_duration_minutes)))

    def validate(self, arguments: dict[str, Any]) -> CalendarCreateInput:
        parsed = CalendarCreateInput.model_validate(arguments)
        # Fail the plan (not the run) on a timezone we can never resolve.
        load_timezone(parsed.timezone, self.default_timezone)
        return parsed

    def execute(self, arguments: CalendarCreateInput) -> dict[str, Any]:
        zone = load_timezone(arguments.timezone, self.default_timezone)
        now = datetime.now(timezone.utc)
        start = self._resolve_start(arguments, zone, now)
        if start is None or start < now:
            return {
                "requires_user_input": True,
                "missing_information": ["start_time"],
                "reason": "start_time_unresolved" if start is None else "start_time_in_the_past",
            }
        end = _parse_iso(arguments.end_time or "") or (start + self.default_duration)
        payload: dict[str, Any] = {
            "request_id": arguments.idempotency_key,
            "owner_user_id": arguments.owner_user_id,
            "provider": arguments.provider or DEFAULT_PROVIDER,
            "title": arguments.title,
            "start_time": _iso(start),
            "end_time": _iso(end),
            "timezone": str(zone),
        }
        if arguments.location:
            payload["location"] = arguments.location
        if arguments.description:
            payload["description"] = arguments.description
        source = arguments.source.model_dump(exclude_none=True)
        if source:
            payload["source"] = source

        try:
            result = self.knowledge.create_calendar_event(payload)
        except CalendarNotBound:
            logger.info(
                "owner %s has no calendar authorization; task waits for input",
                arguments.owner_user_id,
            )
            return {
                "requires_user_input": True,
                "missing_information": ["calendar_authorization"],
                "reason": "calendar_not_bound",
            }
        except CalendarAuthorizationInvalid:
            # Distinct from "not bound": the owner authorized once, but the grant
            # can no longer write a calendar and must be renewed.
            logger.info(
                "owner %s has an unusable calendar authorization; task waits for input",
                arguments.owner_user_id,
            )
            return {
                "requires_user_input": True,
                "missing_information": ["calendar_authorization"],
                "reason": "calendar_authorization_invalid",
            }
        except (KnowledgeForbidden, CalendarRejected) as exc:
            raise PermanentCapabilityError(str(exc)) from exc
        except CalendarResultUnknown as exc:
            raise UnknownExternalResultError(str(exc)) from exc

        return {
            "operation": CAPABILITY_NAME,
            "request_id": payload["request_id"],
            "provider": str(result.get("provider") or payload["provider"]),
            "status": str(result.get("status") or "created"),
            "event_id": str(result.get("event_id") or ""),
            "event_url": str(result.get("event_url") or ""),
            "start_time": payload["start_time"],
            "end_time": payload["end_time"],
        }

    def _resolve_start(
        self, arguments: CalendarCreateInput, zone: ZoneInfo, now: datetime
    ) -> datetime | None:
        explicit = _parse_iso(arguments.start_time or "")
        if explicit is not None:
            return explicit.astimezone(timezone.utc)
        base = _parse_iso(arguments.source.sent_at or "") or now
        parsed = parse_time_expression(arguments.time_expression, base, zone)
        if parsed is None:
            return None
        return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
