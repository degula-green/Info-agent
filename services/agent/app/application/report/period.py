"""The natural week a report covers.

A natural week is Monday 00:00 through Sunday 23:59 in Asia/Shanghai. The
default target is the previous *complete* week, and the window travels with the
report so the document itself says which week it describes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"

# "上N周" runs from one "上" (last week) to four. The quantifier is greedy on
# purpose: on "上上一周" it must consume both "上" characters, otherwise the
# plain "上一周" inside the phrase wins and the report silently covers the wrong
# week.
_LAST_N_WEEKS = re.compile(
    r"(?P<up>上{1,4})(?:一)?(?:[个個])?(?:自然)?(?:周|星期|礼拜)"
)
_THIS_WEEK_MARKERS = ("本周", "这周", "这一周", "这个星期", "本自然周")
_EXPLICIT_RANGE = re.compile(
    r"(?P<start>\d{4}-\d{2}-\d{2})\s*(?:至|到|~|-|—)\s*(?P<end>\d{4}-\d{2}-\d{2})"
)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


@dataclass(frozen=True)
class WeekWindow:
    """A half-open window: ``start`` inclusive, ``end`` exclusive."""

    start: datetime
    end: datetime
    timezone_name: str = DEFAULT_TIMEZONE
    # How many natural weeks back this window sits: 0 is the week holding
    # "now", 1 the previous complete week, 2 the week before that. ``None``
    # means an explicit range with no relative name.
    weeks_ago: int | None = None

    @property
    def _zone(self) -> ZoneInfo:
        return _zone(self.timezone_name)

    def _fmt(self, moment: datetime) -> str:
        return moment.astimezone(self._zone).strftime("%Y-%m-%d")

    @property
    def start_date(self) -> str:
        return self._fmt(self.start)

    @property
    def end_date(self) -> str:
        return self._fmt(self.end - timedelta(seconds=1))

    @property
    def label(self) -> str:
        """``2026-09-28 至 2026-10-04`` — what the report prints."""

        return f"{self.start_date} 至 {self.end_date}"

    @property
    def period_text(self) -> str:
        """The line that tells the reader which week this report covers."""

        return f"{self._relative_label}周报（{self.label}）"

    @property
    def _relative_label(self) -> str:
        if self.weeks_ago is None:
            return ""
        if self.weeks_ago <= 0:
            return "本周"
        if self.weeks_ago == 1:
            return "上一周"
        return "上" * self.weeks_ago + "周"

    @property
    def range_text(self) -> str:
        """A precise window for the answer prompt and the source list."""

        return (
            f"{self.start_date} 00:00 至 {self.end_date} 23:59 "
            f"({self.timezone_name})"
        )

    @property
    def start_iso(self) -> str:
        return self.start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def end_iso(self) -> str:
        return self.end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def default_file_stem(self) -> str:
        return f"{self.start_date}-{self.end_date}"

    def file_name(self, person_name: str) -> str:
        person = "".join(str(person_name or "").split()) or "周报"
        return f"{person}{self.default_file_stem}.docx"


def week_of(moment: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> WeekWindow:
    """The natural week containing ``moment``."""

    zone = _zone(timezone_name)
    local = moment.astimezone(zone)
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=local.weekday()
    )
    return WeekWindow(
        start=monday,
        end=monday + timedelta(days=7),
        timezone_name=timezone_name,
        weeks_ago=0,
    )


def weeks_ago_window(
    now: datetime,
    weeks_ago: int,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> WeekWindow:
    """The natural week ``weeks_ago`` weeks before the one holding ``now``."""

    weeks_ago = max(int(weeks_ago), 0)
    current = week_of(now, timezone_name)
    start = current.start - timedelta(days=7 * weeks_ago)
    return WeekWindow(
        start=start,
        end=start + timedelta(days=7),
        timezone_name=timezone_name,
        weeks_ago=weeks_ago,
    )


def previous_week(
    now: datetime, timezone_name: str = DEFAULT_TIMEZONE
) -> WeekWindow:
    return weeks_ago_window(now, 1, timezone_name)


def resolve_week(
    instruction: str | None,
    *,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> WeekWindow:
    """Map the user's wording onto the week to report on.

    Anything unrecognised falls back to the previous complete week, which is
    the V1 default: a report written on Monday must not describe a week that
    has only just started.
    """

    text = " ".join(str(instruction or "").split())
    explicit = _EXPLICIT_RANGE.search(text)
    if explicit:
        zone = _zone(timezone_name)
        try:
            start = datetime.strptime(explicit.group("start"), "%Y-%m-%d").replace(
                tzinfo=zone
            )
            end_day = datetime.strptime(explicit.group("end"), "%Y-%m-%d").replace(
                tzinfo=zone
            )
        except ValueError:
            return previous_week(now, timezone_name)
        end = end_day + timedelta(days=1)
        if end <= start:
            return previous_week(now, timezone_name)
        return WeekWindow(start=start, end=end, timezone_name=timezone_name)
    if any(marker in text for marker in _THIS_WEEK_MARKERS):
        return week_of(now, timezone_name)
    last_n = _LAST_N_WEEKS.search(text)
    if last_n:
        return weeks_ago_window(now, len(last_n.group("up")), timezone_name)
    return previous_week(now, timezone_name)
