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
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.web.form_browser_client import (
    FormBrowserClient,
    FormBrowserError,
    FormLoginRequired,
)
from app.infrastructure.web.url_tools import trusted_urls
from app.kernel.models import CapabilityDescriptor

PREVIEW_NAME = "form.preview"
APPLY_NAME = "form.apply"

FormAction = Literal["fill_only", "fill_and_submit", "write_cells"]

_SEPARATORS = ":：=,，;；、\t "
_TRAILING = "。.,，;；"


class FormField(BaseModel):
    """One field of the draft, with where the value came from."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str = ""
    source: str = "empty"
    confidence: float = Field(default=0.0, ge=0, le=1)


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


class FormApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=4000)
    draft: FormDraft
    action: FormAction = "write_cells"
    # One row per entry, aligned with ``draft.headers``.
    values: list[list[str]] = Field(min_length=1)
    owner_user_id: str = ""
    idempotency_key: str = Field(min_length=1)


class FormApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    status: str
    action: str
    written_range: str
    observed: list[list[str]] = Field(default_factory=list)
    verified: bool = False


def fingerprint_headers(headers: list[str]) -> str:
    """Stable identity of the sheet layout the owner approved."""

    payload = "|".join(str(item).strip() for item in headers)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_values(headers: list[str], request: str) -> dict[str, str]:
    """Pull ``header: value`` pairs out of the owner's own sentence.

    Longest header first so ``家长姓名`` wins over ``姓名``. Values end where
    the next header begins, which is what makes ``姓名：张三 性别：男`` yield
    two pairs instead of one long string.
    """

    text = str(request or "")
    known = sorted({h for h in headers if h and h.strip()}, key=len, reverse=True)
    if not text or not known:
        return {}
    pattern = "|".join(re.escape(item) for item in known)
    matches = list(re.finditer(pattern, text))
    found: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(0)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[start:end].strip()
        value = raw.lstrip(_SEPARATORS).strip().strip(_TRAILING).strip()
        if value and name not in found:
            found[name] = value
    return found


def build_fields(headers: list[str], values: dict[str, str]) -> tuple[list[FormField], list[str]]:
    fields: list[FormField] = []
    missing: list[str] = []
    for header in headers:
        if not header:
            continue
        value = values.get(header, "")
        if value:
            fields.append(
                FormField(name=header, value=value, source="instruction", confidence=0.9)
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

    def __init__(self, client: FormBrowserClient, *, timeout_seconds: int = 180) -> None:
        self.client = client
        if timeout_seconds:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> FormPreviewInput:
        return FormPreviewInput.model_validate(arguments)

    def execute(self, arguments: FormPreviewInput) -> dict[str, Any]:
        url = _trusted(arguments.url, arguments.request)
        session_id = self.client.create_session()
        try:
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
            if page.get("kind") != "spreadsheet":
                raise FormBrowserError(
                    f"unsupported page kind: {page.get('kind')}",
                    code="unsupported_page_kind",
                )
            grid = self.client.read_grid(session_id)
        except Exception:
            self.client.close_session(session_id)
            raise

        headers = [str(item) for item in (grid.get("headers") or [])]
        rows = grid.get("rows") or []
        values = extract_values(headers, arguments.request)
        fields, missing = build_fields(headers, values)
        # New data goes on the first row after the ones already present.
        target_row = len(rows) + 2
        # The preview does not hand its session to the write step: the approval
        # may sit for minutes, and the write opens a fresh session anyway. Not
        # closing here would leak one Chromium context per preview.
        self.client.close_session(session_id)
        draft = FormDraft(
            draft_id=str(uuid4()),
            form_url=page.get("final_url") or url,
            title=str(page.get("title") or ""),
            kind="spreadsheet",
            write_model="live_document",
            session_id="",
            page_fingerprint=fingerprint_headers(headers),
            headers=headers,
            target_cell=f"A{target_row}" if target_row >= 1 else "A1",
            existing_rows=len(rows),
            fields=fields,
            missing=missing,
            action="write_cells",
        )
        warnings: list[str] = []
        if missing:
            warnings.append("缺少字段：" + "、".join(missing))
        return FormPreviewResult(form=draft, warnings=warnings).model_dump()


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
        session_id = self.client.create_session()
        try:
            page = self.client.open(session_id, url)
            if page.get("login_required"):
                self.client.close_session(session_id)
                return {
                    "requires_user_input": True,
                    "missing_information": ["form_login"],
                    "reason": "目标文档需要登录后才能写入，请先完成登录",
                    "takeover": {"session_id": session_id, "url": url},
                }
            grid = self.client.read_grid(session_id)
            headers = [str(item) for item in (grid.get("headers") or [])]
            if fingerprint_headers(headers) != draft.page_fingerprint:
                raise FormBrowserError(
                    "the sheet layout changed since the preview; a new preview is required",
                    code="page_changed",
                )
            written = self.client.write_grid(session_id, draft.target_cell, arguments.values)
        except FormLoginRequired as exc:
            self.client.close_session(session_id)
            return {
                "requires_user_input": True,
                "missing_information": ["form_login"],
                "reason": str(exc),
                "takeover": {"session_id": session_id, "url": url},
            }
        except Exception:
            self.client.close_session(session_id)
            raise
        self.client.close_session(session_id)
        return FormApplyResult(
            operation=APPLY_NAME,
            status="written" if written.get("verified") else "unverified",
            action=arguments.action,
            written_range=str(written.get("written_range") or ""),
            observed=[list(row) for row in (written.get("observed") or [])],
            verified=bool(written.get("verified")),
        ).model_dump()
