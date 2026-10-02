"""Shared deterministic pre-filter vocabulary.

Used by the ingress pre-filter, the deterministic planner and the time
parser so that "does this text look like a schedule?" can never drift between
the three.

The pre-filter is deliberately narrow: it only drops text that carries no
candidate goal at all (pure social noise). Everything else reaches task
understanding, which owns the real verdict. It lowers the number of useless
Tasks and never replaces task understanding.
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
WEEK_RANGE = r"(?:(?:下下|下|这|本)?(?:周|星期|礼拜))"
CALENDAR_DAY = r"(?:\d{1,2}\s*月\s*\d{1,2}\s*[日号])"
DATE = rf"(?:{DAY_WORD}|{WEEKDAY}|{WEEK_RANGE}|{CALENDAR_DAY})"

# A written time the parser can actually resolve: a date, a period or a clock.
TIME_PHRASE_PATTERN = re.compile(
    rf"(?:{DATE}\s*(?:{PERIOD}\s*)?(?:{CLOCK})?"
    rf"|(?:{PERIOD}\s*)?{CLOCK})"
)

# Signals that the user explicitly asked to take content or parameters from an
# attachment. Matching this against the parsed body would let a document that
# merely mentions "附件" trigger itself, so callers pass the user's own text.
ATTACHMENT_REFERENCE_PATTERN = re.compile(
    r"(?:附件|文档|文件|资料|材料)"
    r"|(?:根据|按照|参考|基于|依据|结合)(?:上述|以上|下面|以下|其中|这个|该|这份)"
)


def references_attachment(text: str | None) -> bool:
    """Whether the user asked to take content or parameters from an attachment."""

    return ATTACHMENT_REFERENCE_PATTERN.search(str(text or "")) is not None


# Text that is *only* social noise cannot carry a candidate goal, so it never
# becomes a Task. This is the single judgement the pre-filter still makes; every
# other message reaches task understanding. Kept short and explicit on purpose:
# a large word list here would silently decide Tasks before the model sees them.
_NOISE_TOKEN = (
    r"(?:"
    r"(?:早上|上午|中午|下午|晚上|今早|今晚)?(?:好)+(?:的|啊|呀|哟|呢|了|啦|吧|哒)*"
    r"|你好|您好|大家好|晚安|早安"
    r"|(?:收到|好嘞|可以|没问题|辛苦|谢谢|多谢|麻烦|明白|了解|知道|行|嗯)"
    r"(?:了|啦|呀|啊|哟|呢|哈|的|吧)*"
    r"|ok|okey|okay"
    r"|(?:哈|呵|嘿|嘻|嗯|哦|噢|啊|呀|哎|唉|诶|额)+"
    # A short question about nothing ("在吗", "在不在") is a greeting, not a
    # request: nothing follows it that the understanding layer could act on.
    r"|在\s*(?:吗|么|不在)"
    r"|[\W_0-9]+"
    r")"
)
# One or more noise tokens: "收到，谢谢" is two of them and is still only noise.
CHITCHAT_ONLY_PATTERN = re.compile(
    rf"^(?:{_NOISE_TOKEN})(?:[\s,，。、!！~.？?]*(?:{_NOISE_TOKEN}))*$",
    re.IGNORECASE,
)


def schedule_hint(text: str | None) -> bool:
    """True when the text carries a schedule keyword or a resolvable time."""

    if not text:
        return False
    if any(keyword in text for keyword in SCHEDULE_KEYWORDS):
        return True
    return TIME_PHRASE_PATTERN.search(text) is not None


def chitchat_only(text: str | None) -> bool:
    """True when the text is nothing but social noise."""

    stripped = str(text or "").strip()
    if not stripped:
        return False
    return CHITCHAT_ONLY_PATTERN.match(stripped) is not None


def task_candidate_hint(text: str | None) -> bool:
    """The ingress gate: does this text deserve a Task at all?

    A schedule hint is deliberately no longer required. A to-do such as
    "完成登录模块代码" carries no time expression and no schedule keyword, and it
    must still reach task understanding instead of being dropped here.
    """

    stripped = str(text or "").strip()
    if not stripped:
        return False
    return not chitchat_only(stripped)
