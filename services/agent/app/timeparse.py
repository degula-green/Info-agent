"""Time-phrase parsing shared by the deterministic planner and todo.create.

The calendar *write* was retired (the Agent's own to-do ledger replaced it), but
the natural-language time parsing it introduced is still required: "明天晚上八点"
has to become a due date on the to-do.

The behaviour is transplanted verbatim from the retired calendar capability --
the same conservative rules, the same "never guess" stance:

* Only the v1 subset resolves. "八点" on its own is ambiguous and yields None,
  so the raw phrase is kept for the owner to fix on the desktop.
* An unknown or unusable time is never an error: the to-do still gets written.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.ingress.vocabulary import NUMERAL

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

# A day without a clock is a deadline. 明天 means "due by the end of 明天", so
# the resolved moment is the last minute of that day rather than midnight (which
# would read as the very start and look overdue almost immediately).
END_OF_DAY = time(hour=23, minute=59)

# "下周" on its own names no weekday: the owner meant "sometime next week".
# The deadline is the end of that week, matching how the header reads.
_WEEK_RANGE_PATTERN = re.compile(r"(?P<prefix>下下|下|这|本)?\s*(?:周|星期|礼拜)(?!\s*[一二三四五六日天])")

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


def load_timezone(name: str | None, fallback: str = "Asia/Shanghai") -> ZoneInfo | None:
    """An unknown zone must not fail a write: the caller drops the timestamp."""

    candidate = (name or fallback or "").strip()
    if not candidate:
        return None
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(value: str | None) -> int | None:
    """Parses 8, 十二 or 四十五; returns None when it is not a number."""

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
    match = _WEEK_RANGE_PATTERN.search(text)
    if match:
        week_start = base_date - timedelta(days=base_date.weekday())
        prefix = match.group("prefix")
        if prefix == "下":
            week_start += timedelta(days=7)
        elif prefix == "下下":
            week_start += timedelta(days=14)
        elif prefix is None and week_start + timedelta(days=6) < base_date:
            week_start += timedelta(days=7)
        return week_start + timedelta(days=6), True
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


def resolve_time_expression(expression: str | None, base: datetime, tz: ZoneInfo) -> datetime | None:
    """Resolves the v1 subset of natural-language times, or returns None."""

    text = str(expression or "").translate(_NORMALISE)
    if not text.strip():
        return None
    local_base = base.astimezone(tz)
    target_date, has_date = _extract_date(text, local_base.date())
    clock, has_clock = _extract_clock(text)
    if has_clock:
        if clock is None:
            # A clock was written but cannot be read ("八点" with no 上午/下午).
            # The day alone must not stand in for it: closing the day would
            # report 23:59 for a meeting the owner said was at eight.
            return None
        if target_date is None:
            if has_date:
                return None
            target_date = local_base.date()
        return datetime(
            target_date.year, target_date.month, target_date.day,
            clock.hour, clock.minute, tzinfo=tz,
        )
    if not has_date or target_date is None:
        # No clock and no day: either nothing was said ("下周" on its own) or it
        # was not a time at all. Nothing to resolve.
        return None
    # A day with no clock is a deadline, not an unknown: 明天 means the work is
    # due by the end of 明天. The desktop can still move the hour.
    return datetime(
        target_date.year, target_date.month, target_date.day,
        END_OF_DAY.hour, END_OF_DAY.minute, tzinfo=tz,
    )


# Historical name: the deterministic planner imported parse_time_expression.
parse_time_expression = resolve_time_expression
