"""Shared deterministic schedule vocabulary.

Used by the ingress pre-filter, the deterministic planner and the calendar
parser so that "does this text look like a schedule?" can never drift between
the three. It is a *pre-filter*: it only lowers the number of useless Tasks and
never replaces task understanding.
"""

from __future__ import annotations

import re

SCHEDULE_KEYWORDS: tuple[str, ...] = (
    "会议",
    "开会",
    "日程",
    "安排",
    "预约",
    "碰一下",
    "碰头",
    "评审",
    "汇报",
    "讨论",
    "聚餐",
    "面试",
    "培训",
    "分享",
    "例会",
    "面谈",
)

CJK_DIGITS = "零〇一二两三四五六七八九十"
NUMERAL = rf"(?:\d{{1,2}}|[{CJK_DIGITS}]{{1,3}})"

PERIOD = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)"
CLOCK = rf"(?:\d{{1,2}}\s*[:：]\s*\d{{2}}|{NUMERAL}\s*点\s*(?:半|{NUMERAL}\s*分?)?)"
DAY_WORD = r"(?:今天|今日|明天|明日|后天|大后天|今晚|明晚|今早)"
WEEKDAY = r"(?:(?:下下|下|这|本)?(?:周|星期|礼拜)[一二三四五六日天])"
CALENDAR_DAY = r"(?:\d{1,2}\s*月\s*\d{1,2}\s*[日号])"
DATE = rf"(?:{DAY_WORD}|{WEEKDAY}|{CALENDAR_DAY})"

# A written time the parser can actually resolve: a date, a period or a clock.
TIME_PHRASE_PATTERN = re.compile(
    rf"(?:{DATE}\s*(?:{PERIOD}\s*)?(?:{CLOCK})?"
    rf"|(?:{PERIOD}\s*)?{CLOCK})"
)


def schedule_hint(text: str | None) -> bool:
    """True when the text carries a schedule keyword or a resolvable time."""

    if not text:
        return False
    if any(keyword in text for keyword in SCHEDULE_KEYWORDS):
        return True
    return TIME_PHRASE_PATTERN.search(text) is not None
