"""form.preview and form.apply.

Two capabilities rather than three command types, because the split that
matters to the kernel is risk and lifecycle:

* ``form.preview`` reads a link and produces a draft. Read-only, no approval,
  safe to retry.
* ``form.apply`` writes the confirmed draft. ``side_effect``, approval-gated,
  and read back afterwards.

The command differences — fill only, fill and submit, write cells — are the
``action`` argument of ``form.apply``, not separate capabilities.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.web.form_browser_client import (
    FormBrowserClient,
    FormBrowserError,
    FormLoginRequired,
)
from app.infrastructure.web.url_tools import trusted_urls
from app.kernel.execution_context import current_execution_context
from app.kernel.models import (
    CapabilityDescriptor,
    CapabilityInputBinding,
    StepOutputRef,
)
from app.capabilities.person_scope import (
    SOURCE_MENTION,
    SOURCE_PRIVATE,
    SOURCE_SENT,
)
from app.understanding.subject import extract_subject_mention, subject_core

PREVIEW_NAME = "form.preview"
APPLY_NAME = "form.apply"

FormAction = Literal["fill_only", "fill_and_submit", "write_cells"]


def _resume_session_id() -> str:
    try:
        value = current_execution_context().task_input.get("resume_session_id")
    except RuntimeError:
        return ""
    return str(value or "").strip()

_SEPARATORS = ":：=,，;；、\t "
_TRAILING = "。.,，;；"
# A field value is a value, not a paragraph. Retrieval in particular returns
# whole messages, and a document must not receive a sentence in a phone-number
# column just because the sentence happened to follow the field's name.
MAX_FIELD_VALUE_CHARS = 60
_SENTENCE_MARKERS = "。！？!?\n"
# One lookup per field, so a wide form does not turn into a search storm. The
# ceiling has to clear real forms: a nine-column class roster lost its last
# column because this was 8.
MAX_RETRIEVAL_FIELDS = 16
# How many ranked hits each scope part contributes per column, and how many
# messages either side of a hit are pulled in as context.
_SCOPE_TOP_K = 50
_CONTEXT_RADIUS = 2
# Particles people put between a label and its value ("学号是2025").
_VALUE_PREFIXES = _SEPARATORS + "是为"

# Instruction clauses that follow a value ("负责人是张三，填写并提交") and
# labels that follow another field inside one retrieved sentence
# ("服务器账号是root，密码lyc302974"). Both are cut so a cell holds a value,
# not the rest of the sentence.
_VALUE_TRAILING_COMMANDS = (
    "填写并提交",
    "确认提交",
    "并提交",
    "然后提交",
    "再提交",
    "填写",
    "提交",
    "并确认",
    "确认",
)
_VALUE_FOLLOWING_KEYS = (
    "密码",
    "账号",
    "电话",
    "手机号",
    "姓名",
    "负责人",
    "项目",
    "地址",
    "编号",
)

# --------------------------------------------------------------------------
# Whose value is this?
#
# A form value is only trustworthy when we know who it belongs to. The owner
# names a subject ("先躺会再说", "我的公司"), or -- when they name nothing --
# the columns themselves say which scope to read from: personal columns come
# from the owner, company columns from the owner's company.
# --------------------------------------------------------------------------
DEFAULT_PERSONAL_SCOPE = "我"
# The corpus holds the bare word. "我的公司" is how a person says it, never how
# a document is filed, so searching for it finds nothing.
DEFAULT_COMPANY_SCOPE = "公司"

# A URL is frequently followed by Chinese punctuation with no space
# ("https://...，帮我把某人..."). Stop at sentence punctuation so stripping the
# link does not also strip the subject and turn the request into "my info".
_URL_IN_TEXT = re.compile(r"https?://[^\s，,。；;、）)】]+", re.IGNORECASE)

_PERSONAL_FIELD_MARKERS = (
    "姓名", "名字", "学号", "学籍", "性别", "年龄", "生日", "出生",
    "手机", "电话", "qq", "微信", "邮箱", "家长", "监护人", "住址",
    "家庭地址", "联系地址", "身份证", "班级", "专业", "学院", "年级",
)
_COMPANY_FIELD_MARKERS = (
    "公司", "企业", "单位", "统一社会信用代码", "信用代码", "税号",
    "注册资本", "法定代表人", "法人", "经营范围", "成立时间",
    "注册地址", "办公地址", "公司地址", "员工", "规模",
)

# Value shapes. The reader accepts whatever follows a label; these decide
# whether that text is actually a value of the column's type.
_MOBILE_NUMBER = re.compile(r"1[3-9]\d{9}")
_LANDLINE_NUMBER = re.compile(r"0\d{2,3}-?\d{7,8}")
_STUDENT_ID = re.compile(r"[A-Za-z]{0,6}\d{4,20}")
_QQ_NUMBER = re.compile(r"[1-9]\d{4,11}")
_PERSON_NAME = re.compile(r"[\u4e00-\u9fa5]{2,4}")
_LATIN_NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{1,30}")
_ADDRESS_MARKER = re.compile(
    r"(省|市|区|县|镇|乡|村|街|路|道|巷|小区|胡同|社区|号|栋|幢|单元|室|楼)"
)
# A locality need not spell out its administrative level: "狗熊岭" is still a
# place. Require a place-like suffix instead of accepting every short Chinese
# phrase, otherwise chat fragments and landmarks pass as home addresses.
_ADDRESS_PLACE_SUFFIX = re.compile(
    r"(岭|庄|寨|屯|堡|苑|园|家园|新村|广场|大厦|公寓|花园|里)$"
)
_ADDRESS_PROSE_MARKERS = (
    "不想",
    "不用",
    "不要",
    "不知道",
    "太远",
    "卡住",
    "怎么办",
    "咋办",
    "怎么回事",
    "咋回事",
    "就是",
    "不是",
    "没有",
    "这个",
    "那个",
    "然后",
    "并且",
)
_ADDRESS_LEAD_STOP = ("的", "了", "是", "在", "和", "与", "或", "这", "那", "就", "都", "也")
_ADDRESS_TRAIL_STOP = ("了", "吧", "吗", "呢", "啊", "呀", "哦", "嘛")


def extract_subject(
    request: str,
    *,
    subject_extractor: Any | None = None,
) -> str:
    """The entity the owner asked to fill in, when they named one."""

    text = _URL_IN_TEXT.sub(" ", str(request or ""))
    mention = extract_subject_mention(
        text,
        intent="form.complete",
        extractor=subject_extractor,
    )
    if mention.kind == "self":
        return "我"
    if mention.kind in {"person", "organization"} and mention.mention:
        return mention.mention
    return ""


def infer_scopes(names: list[str]) -> list[str]:
    """Which scope the columns imply when the owner named no subject."""

    lowered = [str(name or "").lower() for name in names]
    personal = any(
        any(marker in name for marker in _PERSONAL_FIELD_MARKERS) for name in lowered
    )
    company = any(
        any(marker in name for marker in _COMPANY_FIELD_MARKERS) for name in lowered
    )
    scopes: list[str] = []
    if personal or not company:
        scopes.append(DEFAULT_PERSONAL_SCOPE)
    if company:
        scopes.append(DEFAULT_COMPANY_SCOPE)
    return scopes


def resolve_scopes(
    request: str,
    names: list[str],
    *,
    subject_extractor: Any | None = None,
) -> list[str]:
    """The scopes this preview should read from, most specific first."""

    subject = extract_subject(request, subject_extractor=subject_extractor)
    return [subject] if subject else infer_scopes(names)


# How people phrase a value when the column name is not in the sentence:
# "我叫小马" carries 姓名, "住址狗熊岭" carries 家庭住址.
_SPOKEN_FIELD_QUERY: dict[str, tuple[str, ...]] = {
    "姓名": ("我叫", "名字叫"),
}
_FIELD_QUALIFIERS = ("家庭", "公司", "注册", "办公", "联系", "家长")


def field_queries(name: str) -> tuple[str, ...]:
    """The column name plus the short forms it is actually written as.

    A single query for the literal column name misses the way people type:
    nothing in "我叫小马" matches "姓名", and nothing in "住址狗熊岭" matches
    "家庭住址". Two forms per column keeps the lookup honest without turning a
    wide form into a search storm.
    """

    column = str(name or "").strip()
    if not column:
        return ()
    queries: list[str] = [column]
    spoken = _SPOKEN_FIELD_QUERY.get(column)
    if spoken:
        queries.extend(spoken[:1])
    else:
        for prefix in _FIELD_QUALIFIERS:
            if column.startswith(prefix) and len(column) > len(prefix):
                queries.append(column[len(prefix):])
                break
    return tuple(dict.fromkeys(query for query in queries if query))


def validate_field_value(name: str, value: str) -> bool:
    """Type-check a candidate against the column it would be written to.

    Reading a short string after a label is not the same as holding a valid
    phone number or a person's name, and a real write must not carry that
    difference. A rejection here means "leave the cell empty", never "write it
    and hope the owner notices".
    """

    text = str(value or "").strip()
    if not text or not _looks_like_a_value(text):
        return False
    column = str(name or "")
    lowered = column.lower()
    if "电话" in column or "手机" in column:
        return bool(_MOBILE_NUMBER.fullmatch(text) or _LANDLINE_NUMBER.fullmatch(text))
    if "qq" in lowered:
        return bool(_QQ_NUMBER.fullmatch(text))
    if "学号" in column or "学籍" in column:
        return bool(_STUDENT_ID.fullmatch(text))
    if "性别" in column:
        return text in {"男", "女"}
    if "姓名" in column or "名字" in column:
        return bool(_PERSON_NAME.fullmatch(text) or _LATIN_NAME.fullmatch(text))
    if "住址" in column or "地址" in column:
        return _looks_like_address(text)
    if "邮箱" in column or "email" in lowered:
        return "@" in text and "." in text.split("@")[-1]
    return True


def _looks_like_address(text: str) -> bool:
    value = str(text or "").strip()
    if not 2 <= len(value) <= 20:
        return False
    if value.startswith(_ADDRESS_LEAD_STOP):
        return False
    if value.endswith(_ADDRESS_TRAIL_STOP):
        return False
    if any(marker in value for marker in _ADDRESS_PROSE_MARKERS):
        return False
    if _ADDRESS_MARKER.search(value):
        return True
    return bool(_ADDRESS_PLACE_SUFFIX.search(value))


@dataclass(frozen=True)
class KnowledgeHit:
    """A retrieved value together with what makes it attributable."""

    value: str
    subject: str
    tier: str
    evidence: str


def subject_tokens(scope: str) -> tuple[str, ...]:
    """The names a carrier may be filed under for this scope.

    "我的公司" is filed as a document mentioning "公司"; a contact is filed
    under their own name.
    """

    name = str(scope or "").strip()
    if not name:
        return ()
    tokens = [name]
    for prefix in ("我们的", "我们组", "我的", "我司", "我们", "我"):
        if name.startswith(prefix) and len(name) > len(prefix):
            core = name[len(prefix):].strip()
            if core and core not in tokens:
                tokens.append(core)
            break
    return tuple(tokens)


def attribution_tier(
    source: str,
    scope: str,
    chunk: Any,
    subject_names: tuple[str, ...] = (),
) -> str:
    """How strongly one piece of evidence belongs to ``scope``.

    ``source`` says where the value was read from:

    ``sent`` - what he sent. The window *is* his own account, so the window is
            the proof; asking the text to repeat his name would only lose the
            values he typed about himself.
    ``private`` - the 1:1 chats he is part of. His own lines are confirmed;
            the other side's lines are only offered for review, because
            "someone told me X" is evidence about him, not his own statement.
    ``mention`` - the text was matched on the subject's name, so the value is
            only confirmed when the name stands in the same sentence.
    ``name`` - the corpus was searched by the subject's name (a company, or a
            deployment with no identity resolver): ``a`` when the carrier is
            the subject's own chat or document, ``b`` when the name merely
            appears in the text.
    ``review`` - nothing is known about the subject, so every value is offered
            for the owner to check.

    ``c`` - no link at all - is never written.
    """

    # A chunk that travelled with its own source label wins over the caller's
    # default: a candidate pool mixes what he sent, the chats he is in and the
    # messages around them.
    source = str(getattr(chunk, "source_group", "") or source)
    names = {value.strip() for value in subject_names if str(value).strip()}
    role = str(getattr(chunk, "speaker_role", "") or "")
    sender = str(getattr(chunk, "sender_name", "") or "").strip()
    if source == "sent":
        if role == "subject" or not names or sender in names:
            return "a"
        # A context neighbour inside a group chat is not necessarily his line.
        return "b"
    if source == "review":
        return "b"
    name = subject_core(str(scope or "").strip())
    if source == "private":
        if role == "subject":
            return "a"
        if sender and names and sender in names:
            return "a"
        return "b"
    if not name:
        return "b"
    tokens = [token.lower() for token in subject_tokens(name)]
    text = str(getattr(chunk, "text", "") or "").lower()
    if source == "mention":
        return "a" if any(token in text for token in tokens) else "b"
    carriers = [
        str(getattr(chunk, field, "") or "").strip().lower()
        for field in ("title", "conversation_name", "sender_name")
    ]
    if any(token in carrier for token in tokens for carrier in carriers if carrier):
        return "a"
    if any(token in text for token in tokens):
        return "b"
    return "c"


def evidence_line(chunk: Any) -> str:
    """A short "where this came from" line for the review card."""

    carrier = (
        str(getattr(chunk, "conversation_name", "") or "")
        or str(getattr(chunk, "title", "") or "")
        or str(getattr(chunk, "sender_name", "") or "")
        or "知识库"
    )
    sent_at = str(getattr(chunk, "sent_at", "") or "")
    return f"{carrier} · {sent_at[:10]}" if sent_at else carrier

# Collected data is typed by a person, so the label is whatever they happen to
# call the column and it is usually glued straight to the value --
# "学号20251714205", "qq号123456789", "电话13800000001". Each column therefore
# recognises several spellings, matched case-insensitively.
_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("学号", ("学籍号", "学工号", "学号")),
    ("姓名", ("名字叫", "名字", "我叫", "我姓", "姓名")),
    ("性别", ("性别",)),
    ("联系电话", ("联系电话", "联系方式", "电话")),
    ("手机号", ("手机号码", "手机号", "手机")),
    ("QQ号", ("qq号", "qq")),
    ("家长姓名", ("家长姓名", "家长名字", "父母姓名", "监护人")),
    ("家长电话", ("家长电话", "家长手机", "家长联系方式")),
    ("家庭住址", ("家庭住址", "家庭地址", "住址", "地址")),
    # Not a column of every form, but a boundary that matters: people pack it
    # into the same line ("手机号1532…，邮箱a@b.com") and without it the phone
    # column swallowed the rest of the sentence.
    ("邮箱", ("电子邮箱", "邮箱", "email", "e-mail")),
)
# Longest alias first, so 家长电话 is never read as 电话.
_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (alias.lower(), canonical)
            for canonical, aliases in _FIELD_ALIASES
            for alias in aliases
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

@dataclass(frozen=True)
class _FieldRule:
    """How to recognise one field's value when it arrives without a label.

    Collected data is usually just the fact itself ("15325653689" on its own
    line), so requiring ``字段名: 值`` loses almost everything. The trade is
    that the shape has to identify the field, and the match must be the only
    one in the text.
    """

    pattern: str
    confidence: float
    # Reject a match sitting right after one of these, which is what stops
    # "密码123456" from becoming a student id.
    reject_before: tuple[str, ...] = ()
    # Reject a match whose *value* looks like this. A student id that is
    # phone-shaped is almost certainly somebody's mobile number.
    reject_value: str = ""


# Order matters: a phone-shaped number should land in a phone column rather
# than be claimed by the looser id pattern.
_RELAXED_RULES: tuple[tuple[str, _FieldRule], ...] = (
    ("性别", _FieldRule(r"([男女])", 0.8)),
    ("手机号", _FieldRule(r"(1[3-9]\d{9})", 0.85)),
    ("联系电话", _FieldRule(r"(1[3-9]\d{9})", 0.85)),
    ("家长电话", _FieldRule(r"(1[3-9]\d{9})", 0.7)),
    (
        "QQ号",
        _FieldRule(
            r"(?<!\d)([1-9]\d{4,11})(?!\d)",
            0.7,
            ("密码", "口令", "验证码"),
        ),
    ),
    (
        "学号",
        _FieldRule(
            r"(?<!\d)([A-Za-z]{0,4}\d{6,18})(?!\d)",
            0.6,
            reject_before=("密码", "口令", "验证码", "账号密码"),
            reject_value=r"1[3-9]\d{9}",
        ),
    ),
    (
        "姓名",
        _FieldRule(
            r"(?:我叫|我姓|名字叫|姓名是|姓名[:：])\s*([\u4e00-\u9fa5]{2,4})",
            0.6,
        ),
    ),
    (
        # Matched with ``in``, so this also covers 家庭住址 / 联系地址.
        # An administrative marker is required: without it any phrase ending
        # in 号 ("你发个手机号") reads as an address.
        "家庭住址",
        _FieldRule(
            r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}"
            r"(?:省|市|区|县|镇|村|街|路|道|巷|小区)"
            r"[\u4e00-\u9fa5A-Za-z0-9]{0,20}(?:号|栋|室|楼|单元)?)",
            0.6,
        ),
    ),
)


class FormField(BaseModel):
    """One field of the draft, with where the value came from."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # For an HTML form this is the locator the write step uses; a spreadsheet
    # addresses cells by row instead, so it stays empty there.
    ref: str = ""
    value: str = ""
    source: str = "empty"
    confidence: float = Field(default=0.0, ge=0, le=1)
    # A value read from knowledge carries what made it attributable: the scope
    # it was retrieved under, how strong that attribution is ("a" = the
    # resource is the subject's own carrier, "b" = shared resource, "c" =
    # co-occurrence only, never written), and a short "where from" line.
    subject: str = ""
    tier: str = "none"
    evidence: str = ""


class FormDraft(BaseModel):
    """What the owner reviews before anything is written."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    form_url: str
    title: str = ""
    kind: str = "spreadsheet"
    # ``live_document`` means filling *is* writing; ``submit_form`` means a
    # separate submit control decides when the write lands.
    write_model: str = "live_document"
    session_id: str = ""
    page_fingerprint: str = ""
    headers: list[str] = Field(default_factory=list)
    target_cell: str = "A1"
    # Only meaningful for ``submit_form`` pages.
    submit_ref: str = ""
    submit_label: str = ""
    existing_rows: int = 0
    fields: list[FormField] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    action: FormAction = "write_cells"


class FormPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The owner's own sentence. The planner copies it in rather than letting
    # the model paraphrase, so the URL trust check has a real anchor.
    request: str = Field(min_length=1, max_length=4000)
    url: str = Field(min_length=1, max_length=4000)


class FormPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form: FormDraft
    warnings: list[str] = Field(default_factory=list)
    # What the capability did, in the owner's words. The chat renders these
    # under the step so "opened the form / searched the knowledge base /
    # pre-filled" is visible instead of one opaque line.
    stages: list[str] = Field(default_factory=list)
    # The optional subject reader may spend one model call; the Runtime charges
    # whatever the capability reports here against the Task budget.
    model_calls: int = Field(default=0, ge=0)


class FormApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=4000)
    draft: FormDraft
    action: FormAction = "write_cells"
    # One row per entry, aligned with ``draft.headers``. Empty means "take the
    # values the owner confirmed on the draft"; the planner never invents them.
    values: list[list[str]] = Field(default_factory=list)
    owner_user_id: str = ""
    # Derived from the draft when absent, so a retry of the same confirmed
    # draft reuses one identity instead of needing the model to invent a key.
    idempotency_key: str = ""


class FormApplyPlanInput(BaseModel):
    """The Planner-facing shape of form.apply.

    The Planner points at the preview step instead of retyping the draft; the
    binder resolves that pointer into the ``FormDraft`` the capability reads.
    Values are deliberately absent: they come from the draft the owner
    reviewed, not from the model.
    """

    model_config = ConfigDict(extra="forbid")

    action: FormAction = Field(
        default="write_cells",
        description="fill_only / fill_and_submit 适用于有提交键的表单；write_cells 适用于协作表格",
    )


class FormApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    status: str
    action: str
    written_range: str
    # Where the write landed, and what was there before it, so the receipt can
    # offer an undo without the Agent holding browser state.
    target: str = ""
    previous: list[list[str]] = Field(default_factory=list)
    final_url: str = ""
    observed: list[list[str]] = Field(default_factory=list)
    verified: bool = False


def fingerprint_headers(headers: list[str]) -> str:
    """Stable identity of the sheet layout the owner approved."""

    payload = "|".join(str(item).strip() for item in headers)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_values(
    headers: list[str], text: str, *, relaxed: bool = False
) -> dict[str, str]:
    """Read field values out of chat-shaped text.

    Collected data is typed by a person, so the label is whatever they happen
    to call the column and it is usually glued straight to the value:
    "学号20251714205", "qq号123456789", "电话13800000001". Everything is read
    line by line -- one chat message is one line -- so a value can never run
    into the next message, and a label sitting right beside a value is used
    instead of guessing from the value's shape.
    """

    lines = [line.strip() for line in str(text or "").splitlines()]
    names = [str(item) for item in headers if str(item).strip()]
    found: dict[str, str] = {}
    if not names:
        return found
    used: set[str] = set()
    labelled: set[int] = set()
    labels = _labels_for(names)

    for index, line in enumerate(lines):
        if not line:
            continue
        lowered = line.lower()
        # One line can carry several pairs ("姓名：张三 性别：男"), so walk every
        # label on it in the order they appear.
        cursor = 0
        while cursor < len(line):
            hit = _first_label(lowered, cursor, labels)
            if hit is None:
                break
            position, alias, canonical = hit
            labelled.add(index)
            value_start = position + len(alias)
            cut = _next_label_position(lowered, value_start, labels)
            value = (
                line[value_start:cut]
                .lstrip(_VALUE_PREFIXES)
                .strip()
                .strip(_TRAILING)
                .strip()
            )
            value = _clean_value(value)
            header = _target_header(canonical, names)
            if (
                header is not None
                and header not in found
                and value
                and value not in used
                and _looks_like_a_value(value)
            ):
                found[header] = value
                used.add(value)
            cursor = cut if cut > value_start else value_start

    if relaxed:
        _fill_bare_values(names, lines, found, used, labelled)
    return found


def _labels_for(names: list[str]) -> tuple[tuple[str, str], ...]:
    """Every spelling that may sit next to a value, longest first.

    A column is "学号"; an HTML form's label is whatever the page calls it
    ("Customer name:"). People also write "电话" for 联系电话. All of those are
    labels, so they are matched together.
    """

    entries = list(_ALIASES)
    entries.extend((name.lower(), name) for name in names)
    return tuple(sorted(entries, key=lambda item: len(item[0]), reverse=True))


def _first_label(
    lowered: str, start: int, labels: tuple[tuple[str, str], ...]
) -> tuple[int, str, str] | None:
    """The next label on the line from ``start``, longest match at a position."""

    best: tuple[int, str, str] | None = None
    for alias, canonical in labels:
        position = lowered.find(alias, start)
        if position < 0:
            continue
        if (
            best is None
            or position < best[0]
            or (position == best[0] and len(alias) > len(best[1]))
        ):
            best = (position, alias, canonical)
    return best


def _next_label_position(
    lowered: str, start: int, labels: tuple[tuple[str, str], ...]
) -> int:
    """Where the value after a label has to stop."""

    hit = _first_label(lowered, start, labels)
    return hit[0] if hit is not None else len(lowered)


def _target_header(canonical: str, names: list[str]) -> str | None:
    """The column ``canonical`` refers to, preferring an exact name match."""

    for name in names:
        if name == canonical:
            return name
    containing = [name for name in names if canonical in name]
    if not containing:
        return None
    # The shortest container is the more specific column (家庭住址 over 住址).
    return min(containing, key=len)


def _fill_bare_values(
    names: list[str],
    lines: list[str],
    found: dict[str, str],
    used: set[str],
    labelled: set[int],
) -> None:
    """Last resort for a line that carries no label at all.

    A shape is used only when that one line offers exactly one candidate, and
    never for a value another column already claimed *by name*: when the owner
    writes "电话13800000001" the label decides. Reading shapes first is what
    used to push that number into the 手机号 column.
    """

    for index, line in enumerate(lines):
        if not line or index in labelled:
            continue
        for key, rule in _RELAXED_RULES:
            header = _target_header(key, names)
            if header is None or header in found:
                continue
            hits: set[str] = set()
            for match in re.finditer(rule.pattern, line):
                value = match.group(1) if match.groups() else match.group(0)
                before = line[max(0, match.start() - 6) : match.start()]
                if any(marker in before for marker in rule.reject_before):
                    continue
                if rule.reject_value and re.fullmatch(rule.reject_value, value):
                    continue
                if not _looks_like_a_value(value):
                    continue
                hits.add(value)
            hits -= used
            if len(hits) == 1:
                value = hits.pop()
                found[header] = value
                used.add(value)
                break


def _looks_like_a_value(value: str) -> bool:
    """Whether a candidate reads as a single field value.

    ``extract_values`` works on the owner's terse instruction and on retrieved
    messages, and the latter are prose. Anything that long, or that ends a
    sentence, is not something to put in a cell.
    """

    text = str(value or "").strip()
    if not text or len(text) > MAX_FIELD_VALUE_CHARS:
        return False
    return not any(marker in text for marker in _SENTENCE_MARKERS)


def _clean_value(value: str) -> str:
    """Keep only the field value, not the clause that follows it."""

    text = str(value or "").strip()
    for marker in _VALUE_TRAILING_COMMANDS:
        index = text.find(marker)
        if index > 0:
            text = text[:index].rstrip(" ，,。.;；:：")
            break
    for marker in _VALUE_FOLLOWING_KEYS:
        match = re.search(rf"[，,、;；\s]+{re.escape(marker)}", text)
        if match and match.start() > 0:
            text = text[: match.start()].strip()
            break
    return text


def build_fields(
    headers: list[str],
    values: dict[str, str],
    *,
    sources: dict[str, str] | None = None,
    knowledge: dict[str, "KnowledgeHit"] | None = None,
) -> tuple[list[FormField], list[str]]:
    """One field per header, marking where a value came from.

    A value the instruction supplied outranks one only the knowledge base
    could answer, so callers merge in that order and pass ``sources`` for the
    ones retrieval contributed.
    """

    origin = sources or {}
    hits = knowledge or {}
    fields: list[FormField] = []
    missing: list[str] = []
    for header in headers:
        if not header:
            continue
        value = values.get(header, "")
        if value:
            hit = hits.get(header)
            fields.append(
                FormField(
                    name=header,
                    value=value,
                    source=origin.get(header, "instruction"),
                    confidence=(
                        0.9
                        if origin.get(header) != "knowledge"
                        else (0.8 if hit is not None and hit.tier == "a" else 0.6)
                    ),
                    subject=hit.subject if hit is not None else "",
                    tier=hit.tier if hit is not None else "none",
                    evidence=hit.evidence if hit is not None else "",
                )
            )
        else:
            fields.append(FormField(name=header, value="", source="empty", confidence=0.0))
            missing.append(header)
    return fields, missing


def _trusted(url: str, request: str) -> str:
    """Only a link the owner actually wrote may be opened."""

    allowed = trusted_urls([url], request)
    if not allowed:
        raise FormBrowserError(
            "the link was not present in the owner's request", code="untrusted_url"
        )
    return allowed[0]


_NO_SUBMIT = ("不要提交", "不用提交", "先不提交", "不提交", "别提交", "仅填写", "只填写")


def wants_submit(request: str) -> bool:
    """Whether the owner asked for the submit control to be pressed too.

    Filling and submitting are different amounts of trust, so the default is
    the smaller one: an explicit negation wins, and an explicit ask is needed.
    """

    text = str(request or "")
    if any(marker in text for marker in _NO_SUBMIT):
        return False
    return "提交" in text or "submit" in text.lower()


class FormPreviewCapability:
    """Reads the link and proposes a draft; it never writes."""

    descriptor = CapabilityDescriptor(
        name=PREVIEW_NAME,
        description=(
            "打开用户给出的链接，识别要填写的表单或表格字段，并生成可预览、"
            "可编辑的草稿（只读，不写入任何内容）。request 由系统填入用户"
            "原话，不需要（也不应该）由你提供。"
        ),
        input_schema=FormPreviewInput.model_json_schema(),
        output_schema=FormPreviewResult.model_json_schema(),
        task_text_argument="request",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        reconcilable=True,
        timeout_seconds=180,
    )

    def __init__(
        self,
        client: FormBrowserClient,
        *,
        retriever: Any | None = None,
        value_extractor: Any | None = None,
        subject_extractor: Any | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.client = client
        # Optional: without a retriever the draft simply comes from the
        # instruction and the page, which is still a complete draft.
        self.retriever = retriever
        # Optional second reader: when the label grammar cannot read a value
        # out of the scoped evidence, one model call may. Its answer goes
        # through the same type and attribution checks as a rule match.
        self.value_extractor = value_extractor
        # Optional subject reader: the form intent is known by the time this
        # capability runs, so the model is asked only for whose data to fill.
        self.subject_extractor = subject_extractor
        if timeout_seconds:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> FormPreviewInput:
        return FormPreviewInput.model_validate(arguments)

    def _collect_from_knowledge(
        self, names: list[str], values: dict[str, str], request: str
    ) -> tuple[dict[str, KnowledgeHit], list[str], list[str], list[str]]:
        """Read the gaps from the scope this preview is about.

        The subject is resolved to a set of resources first, and values are
        then read strictly inside that set, so a value's owner follows from the
        search window instead of being guessed from prose. Returns the hits and
        the scopes that could not be resolved at all.
        """

        if self.retriever is None:
            return {}, [], [], []
        pending = [name for name in names if name and name not in values]
        if not pending:
            return {}, [], [], []

        found: dict[str, KnowledgeHit] = {}
        unresolved: list[str] = []
        truncated: list[str] = []
        strict_scopes: list[str] = []
        used: set[str] = set()
        for scope in resolve_scopes(
            request,
            names,
            subject_extractor=self.subject_extractor,
        ):
            remaining = [name for name in pending if name not in found]
            if not remaining:
                break
            plan = self._scope_plan(scope, names)
            if plan is None:
                # The owner named somebody the knowledge base does not hold.
                # Leave the columns empty and say so rather than falling back
                # to whoever matches the column name.
                unresolved.append(scope)
                continue
            kind, payload, subject_names, scope_truncated, recent_pool = plan
            subject_only = bool(subject_names)
            if subject_only:
                strict_scopes.append(scope)
            if scope_truncated:
                truncated.append(scope)
            scope_hits = 0
            for name in remaining[:MAX_RETRIEVAL_FIELDS]:
                if kind == "exported":
                    # The complete window is already in hand. For a person,
                    # only the subject's own messages may supply a value;
                    # other lines remain context.
                    candidates = tuple(
                        chunk
                        for chunk in payload
                        if not subject_only
                        or self._is_subject_authored(chunk, subject_names)
                    )
                else:
                    candidates = self._candidates_for_column(
                        name,
                        names,
                        payload,
                        recent_pool,
                        scope,
                        subject_names,
                        subject_only,
                    )
                hit = (
                    self._hit_for_field(
                        name,
                        names,
                        candidates,
                        scope,
                        "name",
                        subject_names,
                        subject_only=subject_only,
                    )
                    if candidates
                    else None
                )
                if hit is None or hit.value in used:
                    continue
                found[name] = hit
                used.add(hit.value)
                scope_hits += 1
            if scope_hits == 0 and (
                scope != DEFAULT_PERSONAL_SCOPE or subject_only
            ):
                # The subject resolved to resources, but none of them could
                # supply an attributable value. The owner still needs to hear
                # that, exactly as when the subject resolved to nothing.
                unresolved.append(scope)
        return found, unresolved, truncated, strict_scopes

    def _scope_plan(
        self, scope: str, names: list[str]
    ) -> (
        tuple[
            list[tuple[str, dict[str, tuple[str, ...]], tuple[str, ...], str]],
            tuple[str, ...],
            bool,
            tuple[Any, ...],
        ]
        | None
    ):
        """The filter parts that make up this scope, or None when unknown.

        Each part is ``(source, filters, resource ids, query prefix)``: the
        filters describe the whole window to the search (who sent it / which
        chat / which phrase), while the ids keep a plain id-restricted search
        working for deployments and tests without the filter form.
        """

        if (
            scope == DEFAULT_PERSONAL_SCOPE
            and getattr(self.retriever, "person_scope", None) is None
        ):
            # No identity resolver in this deployment: nothing is known about
            # "我", so read the whole library and mark every value for review.
            return (
                "retrieval",
                [("review", {}, (), "")],
                (),
                False,
                (),
            )
        resolution = self._resolve_scope(scope)
        if resolution is None or not resolution.resource_ids:
            return None
        groups = dict(getattr(resolution, "groups", ()) or ())
        subject_names = tuple(getattr(resolution, "subject_names", ()) or ())
        subject_only = bool(subject_names)
        truncated = bool(getattr(resolution, "truncated", False))
        anchors = tuple(getattr(resolution, "anchors", ()) or ())
        exported = tuple(getattr(resolution, "chunks", ()) or ())
        if exported and not truncated:
            # The resolver already exported the whole window. Reading that is
            # both complete and cheaper than issuing a ranked query per column:
            # each RAG call costs ~2-3s here, so the 37 export pages beat
            # 4-5 calls per column.
            return ("exported", exported, subject_names, truncated, ())

        parts: list[tuple[str, dict[str, tuple[str, ...]], tuple[str, ...], str]] = []
        sent_ids = tuple(groups.get(SOURCE_SENT, ()))
        if sent_ids:
            parts.append((SOURCE_SENT, {"sender_ids": sent_ids}, sent_ids, ""))
        private_ids = tuple(groups.get(SOURCE_PRIVATE, ()))
        if private_ids:
            parts.append(
                (SOURCE_PRIVATE, {"conversation_ids": private_ids}, private_ids, "")
            )
        mention_ids = tuple(groups.get(SOURCE_MENTION, ()))
        if mention_ids:
            # One part per platform name: the phrase filter pins the anchor, and
            # the id-restricted fallback still needs the name in its query.
            for anchor in anchors or ((subject_core(scope) or scope),):
                parts.append(
                    (
                        SOURCE_MENTION,
                        {"content_contains": (anchor,)},
                        mention_ids,
                        anchor,
                    )
                )
        if not parts:
            parts.append(
                (
                    "name",
                    {"resource_ids": tuple(resolution.resource_ids)},
                    tuple(resolution.resource_ids),
                    subject_core(scope) or scope,
                )
            )
        if subject_only:
            parts = [part for part in parts if part[0] == SOURCE_SENT]
        return (
            "retrieval",
            parts,
            subject_names,
            truncated,
            self._recent_pool(parts, names, subject_only=subject_only),
        )

    def _recent_pool(
        self,
        parts: list[tuple[str, dict[str, tuple[str, ...]], tuple[str, ...], str]],
        names: list[str],
        *,
        subject_only: bool = False,
    ) -> tuple[Any, ...]:
        """A bounded "newest in scope" pool, so bare values still get a chance.

        Some values carry no field word at all (a lone "小呆呆" in a chat), and
        a ranked query cannot be relied on to surface them. The newest window
        per source costs one small call each and is scanned by every column.
        """

        fetcher = getattr(self.retriever, "recent_scope_resource_ids", None)
        if fetcher is None:
            return ()
        query = " ".join(str(name) for name in names if name)[:200].strip()
        if not query:
            return ()
        pool: list[Any] = []
        for source, filters, _, _ in parts:
            if "resource_ids" in filters:
                continue
            try:
                recent_ids = fetcher(**filters, limit=_SCOPE_TOP_K)
            except Exception:  # noqa: BLE001 - retrieval is best effort
                continue
            if not recent_ids:
                continue
            chunks = self.retriever.search(query, recent_ids, top_k=_SCOPE_TOP_K)
            pool.extend(self._tag(chunks, source))
        return tuple(pool)

    def _candidates_for_column(
        self,
        name: str,
        names: list[str],
        parts: list[tuple[str, dict[str, tuple[str, ...]], tuple[str, ...], str]],
        recent_pool: tuple[Any, ...],
        scope: str,
        subject_names: tuple[str, ...] = (),
        subject_only: bool = False,
    ) -> tuple[Any, ...]:
        """The candidate evidence for one column.

        Top-k from a hybrid search inside each scope part, plus the bounded
        newest window, plus a mandatory field-word pass, plus the messages
        around the strongest hits. The field-word and newest passes are the
        guard rail: a ranked list alone once dropped the only message that held
        the value.
        """

        # The subject's name joins only the "mentioned him" window: his own
        # messages rarely repeat it, and carrying it would lose recall there.
        plain_query = " ".join(field_queries(name)).strip()
        named_query = " ".join(
            [subject_core(scope) or "", *field_queries(name)]
        ).strip()
        pool: list[Any] = list(recent_pool)
        search_parts = [
            part
            for part in parts
            if not subject_only or part[0] == SOURCE_SENT
        ]
        for source, filters, ids, prefix in search_parts:
            query = named_query if source == SOURCE_MENTION else plain_query
            pool.extend(self._part_search(query, source, filters, ids, prefix))
        if not any(self._has_field_word(chunk, name) for chunk in pool):
            for source, filters, ids, prefix in search_parts:
                query = (
                    f"{prefix} {name}".strip()
                    if source == SOURCE_MENTION and prefix
                    else name
                )
                pool.extend(
                    self._part_search(query, source, filters, ids, "")
                )
        if subject_only:
            pool = [
                chunk
                for chunk in pool
                if self._is_subject_authored(chunk, subject_names)
            ]
        pool = list(self._dedupe(pool))
        pool.extend(self._context(pool, parts))
        return tuple(self._dedupe(pool))

    def _part_search(
        self,
        query: str,
        source: str,
        filters: dict[str, tuple[str, ...]],
        ids: tuple[str, ...],
        prefix: str,
    ) -> list[Any]:
        text = str(query or "").strip()
        if not text:
            return []
        searcher = getattr(self.retriever, "search_scope", None)
        if searcher is not None and "resource_ids" not in filters:
            try:
                chunks = searcher(text, top_k=_SCOPE_TOP_K, **filters)
            except Exception:  # noqa: BLE001 - retrieval is best effort
                chunks = []
            return self._tag(chunks, source)
        scoped = text
        if prefix and prefix.lower() not in text.lower():
            scoped = f"{prefix} {text}".strip()
        return self._tag(
            self.retriever.search(scoped, ids, top_k=_SCOPE_TOP_K), source
        )

    def _context(
        self,
        pool: list[Any],
        parts: list[tuple[str, dict[str, tuple[str, ...]], tuple[str, ...], str]],
    ) -> list[Any]:
        """Messages around the pool's anchors, tagged with the anchor's source."""

        fetcher = getattr(self.retriever, "scope_context", None)
        if fetcher is None:
            return []
        conversation_source: dict[str, str] = {}
        for source, filters, _, _ in parts:
            for conversation_id in filters.get("conversation_ids", ()):
                conversation_source.setdefault(conversation_id, source)
        anchors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in pool:
            resource_id = str(getattr(chunk, "resource_id", "") or "")
            conversation_id = str(getattr(chunk, "conversation_id", "") or "")
            sent_at = str(getattr(chunk, "sent_at", "") or "")
            if not resource_id or not conversation_id or not sent_at:
                continue
            if resource_id in seen:
                continue
            seen.add(resource_id)
            anchors.append(
                {
                    "resource_id": resource_id,
                    "conversation_id": conversation_id,
                    "sent_at": sent_at,
                }
            )
        if not anchors:
            return []
        owner_user_id, organization_id, request_id, trace_id = self._context_identity()
        chunks = fetcher(
            anchors[:50],
            radius=_CONTEXT_RADIUS,
            owner_user_id=owner_user_id,
            organization_id=organization_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        tagged: list[Any] = []
        for chunk in chunks or ():
            conversation_id = str(getattr(chunk, "conversation_id", "") or "")
            # A neighbour of a "he sent it" hit lives in whatever conversation
            # that message came from (often a group); the tier rule keeps it
            # confirmed only when the sender is actually the subject.
            source = conversation_source.get(conversation_id, SOURCE_SENT)
            tagged.append(
                self._tag([chunk], source, assume_subject_author=False)[0]
            )
        return tagged

    @staticmethod
    def _context_identity() -> tuple[str, str | None, str, str]:
        try:
            context = current_execution_context()
        except RuntimeError:
            return "", None, "", ""
        return (
            context.owner_user_id,
            context.organization_id,
            context.request_id,
            context.trace_id,
        )

    @staticmethod
    def _tag(
        chunks: Any,
        source: str,
        *,
        assume_subject_author: bool = True,
    ) -> list[Any]:
        tagged: list[Any] = []
        for chunk in chunks or ():
            if getattr(chunk, "source_group", ""):
                tagged.append(chunk)
            elif source:
                updates: dict[str, str] = {"source_group": source}
                if (
                    assume_subject_author
                    and source == SOURCE_SENT
                    and not getattr(chunk, "speaker_role", "")
                ):
                    updates["speaker_role"] = "subject"
                tagged.append(replace(chunk, **updates))
            else:
                tagged.append(chunk)
        return tagged

    @staticmethod
    def _is_subject_authored(
        chunk: Any, subject_names: tuple[str, ...]
    ) -> bool:
        """Whether this chunk is a message the target person wrote."""

        source = str(getattr(chunk, "source_group", "") or "")
        if source == SOURCE_MENTION:
            # A mention search is a search over other people's text. Even
            # when a connector labels the sender as the subject, it is not
            # the subject's own statement and must not fill a personal field.
            return False
        role = str(getattr(chunk, "speaker_role", "") or "")
        if role:
            return role == "subject"
        sender = str(getattr(chunk, "sender_name", "") or "").strip()
        names = {str(name).strip() for name in subject_names if str(name).strip()}
        return bool(sender and sender in names)

    @staticmethod
    def _has_field_word(chunk: Any, name: str) -> bool:
        text = str(getattr(chunk, "text", "") or "").lower()
        if not text:
            return False
        return any(query.lower() in text for query in field_queries(name))

    @staticmethod
    def _dedupe(chunks: Any) -> list[Any]:
        seen: set[str] = set()
        unique: list[Any] = []
        for chunk in chunks or ():
            key = f"{getattr(chunk, 'resource_id', '')}:{getattr(chunk, 'text', '')}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(chunk)
        return unique

    def _resolve_scope(self, scope: str):
        try:
            context = current_execution_context()
            return self.retriever.resolve_scope(
                scope,
                owner_user_id=context.owner_user_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                organization_id=context.organization_id,
            )
        except RuntimeError:
            # No execution context (a direct call in a test or a script).
            try:
                return self.retriever.resolve_scope(scope)
            except Exception:  # noqa: BLE001 - retrieval is best effort by design
                return None
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return None

    def _hit_for_field(
        self,
        name: str,
        names: list[str],
        chunks: Any,
        scope: str,
        source: str = "name",
        subject_names: tuple[str, ...] = (),
        *,
        subject_only: bool = False,
    ) -> KnowledgeHit | None:
        """One field's value out of the scoped evidence, or nothing.

        Two readers in order: the label grammar first (cheap and exact), then a
        single model read per chunk when the grammar finds nothing. Both
        answers pass the same type check and attribution tier, so the second
        reader cannot smuggle in a value the first one would have rejected.

        When several pieces of evidence carry a value, the newest one wins: an
        old record must not outrank what the owner typed most recently.
        """

        candidates = [chunk for chunk in (chunks or []) if getattr(chunk, "text", "")]
        best: KnowledgeHit | None = None
        best_at = ""

        def consider(chunk: Any, value: str) -> None:
            nonlocal best, best_at
            if subject_only and not self._is_subject_authored(
                chunk, subject_names
            ):
                return
            tier = attribution_tier(source, scope, chunk, subject_names)
            if tier == "c":
                return
            sent_at = str(getattr(chunk, "sent_at", "") or "")
            if best is not None and sent_at <= best_at:
                return
            best = KnowledgeHit(
                value=value,
                subject=scope,
                tier=tier,
                evidence=evidence_line(chunk),
            )
            best_at = sent_at

        for chunk in candidates:
            # Read with the whole header list: a label decides which column a
            # value belongs to, and where the next one begins. The relaxed
            # reader also accepts a bare value on its own line ("你发个手机号
            # \n15325653689"), which is how people actually type these;
            # validate_field_value below is what keeps junk out.
            value = extract_values(names, chunk.text, relaxed=True).get(name, "")
            if value and validate_field_value(name, value):
                consider(chunk, value)
        if best is not None or self.value_extractor is None:
            return best

        for chunk in candidates:
            try:
                raw = self.value_extractor(name, chunk.text)
            except Exception:  # noqa: BLE001 - the rule reader already failed
                continue
            value = str(raw or "").strip()
            if value and validate_field_value(name, value):
                consider(chunk, value)
        return best

    def execute(self, arguments: FormPreviewInput) -> dict[str, Any]:
        if self.subject_extractor is not None:
            try:
                self.subject_extractor.last_call_count = 0
            except Exception:  # noqa: BLE001 - optional test doubles need no counter
                pass
        url = _trusted(arguments.url, arguments.request)
        resume_session_id = _resume_session_id()
        session_id = resume_session_id or self.client.create_session()
        reused_session = bool(resume_session_id)
        try:
            try:
                page = self.client.open(session_id, url)
            except FormBrowserError:
                if not reused_session:
                    raise
                session_id = self.client.create_session()
                reused_session = False
                page = self.client.open(session_id, url)
            if page.get("login_required"):
                # Not a failure: the owner has to sign in, then we continue.
                return {
                    "requires_user_input": True,
                    "missing_information": ["form_login"],
                    "reason": "目标文档需要登录后才能编辑，请先完成登录",
                    "takeover": {
                        "session_id": session_id,
                        "url": page.get("final_url") or url,
                    },
                }
            kind = str(page.get("kind") or "unknown")
            if kind == "spreadsheet":
                grid = self.client.read_grid(session_id)
            elif kind == "form":
                snapshot = self.client.read_form(session_id)
            else:
                raise FormBrowserError(
                    f"unsupported page kind: {kind}", code="unsupported_page_kind"
                )
        except Exception:
            if not reused_session:
                self.client.close_session(session_id)
            raise

        # The preview does not hand its session to the write step: the approval
        # may sit for minutes, and the write opens a fresh session anyway. Not
        # closing here would leak one Chromium context per preview.
        if not reused_session:
            self.client.close_session(session_id)

        if kind == "spreadsheet":
            headers = [str(item) for item in (grid.get("headers") or [])]
            rows = grid.get("rows") or []
            values = extract_values(headers, arguments.request)
            hits, unresolved, truncated, strict_scopes = self._collect_from_knowledge(
                headers, values, arguments.request
            )
            values.update({name: hit.value for name, hit in hits.items()})
            fields, missing = build_fields(
                headers,
                values,
                sources={name: "knowledge" for name in hits},
                knowledge=hits,
            )
            # New data goes on the first row after the ones already present.
            target_row = len(rows) + 2
            draft = FormDraft(
                draft_id=str(uuid4()),
                form_url=page.get("final_url") or url,
                title=str(page.get("title") or ""),
                kind="spreadsheet",
                write_model="live_document",
                session_id=session_id if reused_session else "",
                page_fingerprint=fingerprint_headers(headers),
                headers=headers,
                target_cell=f"A{target_row}" if target_row >= 1 else "A1",
                existing_rows=len(rows),
                fields=fields,
                missing=missing,
                action="write_cells",
            )
        else:
            raw_fields = snapshot.get("fields") or []
            names = [
                str(item.get("label") or item.get("name") or item.get("ref") or "")
                for item in raw_fields
            ]
            values = extract_values(names, arguments.request)
            hits, unresolved, truncated, strict_scopes = self._collect_from_knowledge(
                names, values, arguments.request
            )
            values.update({name: hit.value for name, hit in hits.items()})
            fields = []
            missing = []
            for item, name in zip(raw_fields, names):
                value = values.get(name, "")
                hit = hits.get(name)
                source = "knowledge" if hit is not None else ("instruction" if value else "empty")
                fields.append(
                    FormField(
                        name=name,
                        ref=str(item.get("ref") or ""),
                        value=value,
                        source=source,
                        confidence=(
                            0.9
                            if value and source == "instruction"
                            else (
                                (0.8 if hit.tier == "a" else 0.6)
                                if hit is not None and value
                                else 0.0
                            )
                        ),
                        subject=hit.subject if hit is not None else "",
                        tier=hit.tier if hit is not None else "none",
                        evidence=hit.evidence if hit is not None else "",
                    )
                )
                if not value:
                    missing.append(name)
            draft = FormDraft(
                draft_id=str(uuid4()),
                form_url=page.get("final_url") or url,
                title=str(page.get("title") or ""),
                kind="form",
                write_model="submit_form",
                session_id=session_id if reused_session else "",
                # The layout identity is the set of controls, not their labels.
                page_fingerprint=fingerprint_headers(
                    [str(field.ref) for field in fields]
                ),
                headers=names,
                target_cell="",
                submit_ref=str(snapshot.get("submit_ref") or ""),
                submit_label=str(snapshot.get("submit_label") or ""),
                fields=fields,
                missing=missing,
                action=(
                    "fill_and_submit"
                    if wants_submit(arguments.request)
                    else "fill_only"
                ),
            )
        warnings: list[str] = []
        if missing:
            if strict_scopes:
                warnings.append(
                    "未找到由「"
                    + "、".join(dict.fromkeys(strict_scopes))
                    + "」本人发出的可用信息，以下字段留空："
                    + "、".join(missing)
                )
            else:
                warnings.append("缺少字段：" + "、".join(missing))
        for scope in unresolved:
            if scope in strict_scopes:
                continue
            else:
                warnings.append(f"未找到「{scope}」的信息")
        if truncated:
            # The scope was wider than the export cap. Say so instead of
            # pretending the empty columns mean "there is no such information".
            warnings.append("资料过多，部分来源未取全（结果可能不完整）")
        filled = sum(1 for field in fields if field.value)
        review = sum(1 for field in fields if field.tier == "b")
        stages = [
            f"已打开「{draft.title or '表单'}」，识别到 {len(draft.headers)} 个字段",
            (
                f"已按主体检索知识库，取到 {len(hits)} 条可用信息"
                if hits
                else "已检索知识库，未找到可用信息"
            ),
            f"预填写完成：{filled} 项已填 · {len(missing)} 项待补"
            + (f" · {review} 项待核对" if review else ""),
        ]
        subject_model_calls = max(
            int(getattr(self.subject_extractor, "last_call_count", 0) or 0),
            0,
        )
        return FormPreviewResult(
            form=draft,
            warnings=warnings,
            stages=stages,
            model_calls=subject_model_calls,
        ).model_dump()


class FormApplyCapability:
    """Writes the confirmed draft; approval is required by the descriptor."""

    descriptor = CapabilityDescriptor(
        name=APPLY_NAME,
        description=(
            "把用户确认过的表单草稿写入目标页面或协作表格。action 取 "
            "fill_only（只填不提交）、fill_and_submit（填并提交）或 "
            "write_cells（写协作表格单元格）。写入前会重新读取页面校验，"
            "页面变化时拒绝写入。"
        ),
        input_schema=FormApplyInput.model_json_schema(),
        planner_input_schema=FormApplyPlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="draft_ref",
                runtime_argument="draft",
                source_capability=PREVIEW_NAME,
                source_output="form",
            ),
        ],
        output_schema=FormApplyResult.model_json_schema(),
        task_text_argument="request",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        reconcilable=True,
        timeout_seconds=120,
    )

    def __init__(self, client: FormBrowserClient, *, timeout_seconds: int = 120) -> None:
        self.client = client
        if timeout_seconds:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> FormApplyInput:
        return FormApplyInput.model_validate(arguments)

    def execute(self, arguments: FormApplyInput) -> dict[str, Any]:
        draft = arguments.draft
        url = _trusted(draft.form_url, arguments.request)
        # A fresh session: the approval may sit for minutes, and the stored
        # login state re-authenticates anyway. The fingerprint is what ties
        # this write back to the page the owner actually reviewed.
        resume_session_id = _resume_session_id()
        session_id = str(draft.session_id or resume_session_id or "").strip()
        reused_session = bool(session_id)
        if not session_id:
            session_id = self.client.create_session()
        try:
            try:
                page = self.client.open(session_id, url)
            except FormBrowserError:
                if not reused_session:
                    raise
                session_id = self.client.create_session()
                reused_session = False
                page = self.client.open(session_id, url)
            if page.get("login_required"):
                return {
                    "requires_user_input": True,
                    "missing_information": ["form_login"],
                    "reason": "目标文档需要登录后才能写入，请先完成登录",
                    "takeover": {"session_id": session_id, "url": url},
                }
            if draft.kind == "form":
                result = self._apply_form(session_id, draft, arguments)
            else:
                result = self._apply_cells(session_id, draft, arguments)
        except FormLoginRequired as exc:
            return {
                "requires_user_input": True,
                "missing_information": ["form_login"],
                "reason": str(exc),
                "takeover": {"session_id": session_id, "url": url},
            }
        except Exception:
            if not reused_session:
                self.client.close_session(session_id)
            raise
        self.client.close_session(session_id)
        return result

    def _apply_cells(
        self, session_id: str, draft: FormDraft, arguments: FormApplyInput
    ) -> dict[str, Any]:
        """Write the confirmed row into a collaborative sheet."""

        # The draft is the single source of truth for what gets written: it is
        # what the owner reviewed and edited. A planner that tries to hand over
        # its own ``values`` (raw retrieval chunks, say) is ignored rather than
        # trusted, because only the approved draft carries consent.
        values = [[field.value for field in draft.fields]]
        # A live document has no submit control, so the write *is* the action.
        # The draft knows the page kind; the planner's guess does not.
        action: str = (
            "write_cells" if draft.write_model == "live_document" else arguments.action
        )
        grid = self.client.read_grid(session_id)
        headers = [str(item) for item in (grid.get("headers") or [])]
        if fingerprint_headers(headers) != draft.page_fingerprint:
            raise FormBrowserError(
                "the sheet layout changed since the preview; a new preview is required",
                code="page_changed",
            )
        written = self.client.write_grid(session_id, draft.target_cell, values)
        return FormApplyResult(
            operation=APPLY_NAME,
            status="written" if written.get("verified") else "unverified",
            action=action,
            written_range=str(written.get("written_range") or ""),
            target=draft.target_cell,
            previous=[list(row) for row in (written.get("previous") or [])],
            observed=[list(row) for row in (written.get("observed") or [])],
            verified=bool(written.get("verified")),
        ).model_dump()

    def _apply_form(
        self, session_id: str, draft: FormDraft, arguments: FormApplyInput
    ) -> dict[str, Any]:
        """Fill an ordinary form, and press submit only when that was asked."""

        snapshot = self.client.read_form(session_id)
        refs = [str(item.get("ref") or "") for item in (snapshot.get("fields") or [])]
        if fingerprint_headers(refs) != draft.page_fingerprint:
            raise FormBrowserError(
                "the form changed since the preview; a new preview is required",
                code="page_changed",
            )

        by_ref = {
            str(field.ref): str(field.value)
            for field in draft.fields
            if field.ref
        }
        # Same rule as the sheet path: only the reviewed draft may supply values.
        by_ref = {ref: value for ref, value in by_ref.items() if value != ""}

        filled = self.client.fill_form(session_id, by_ref)
        failed = filled.get("failed") or {}
        if failed:
            raise FormBrowserError(
                "some fields could not be filled: " + "; ".join(sorted(failed)),
                code="field_fill_failed",
            )

        # A form has no submit control, the draft says fill_and_submit, and the
        # owner confirmed that button: only then is it pressed.
        submitted = draft.write_model == "submit_form" and arguments.action == "fill_and_submit"
        final_url = ""
        if submitted:
            outcome = self.client.submit_form(session_id, draft.submit_ref)
            final_url = str(outcome.get("final_url") or "")
        return FormApplyResult(
            operation=APPLY_NAME,
            status="submitted" if submitted else "filled",
            action="fill_and_submit" if submitted else "fill_only",
            written_range="",
            final_url=final_url,
            observed=[[str(item) for item in by_ref.values()]],
            verified=True,
        ).model_dump()
