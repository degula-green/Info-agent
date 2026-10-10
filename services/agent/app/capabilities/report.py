"""report.weekly: turn one person's archive into a .docx that follows a template.

The capability composes the pieces the plan fixes: the natural-week window, the
person's three sources, the template the user chose (or the one found in the
collected documents), and the rendered document. When no template can be found
it says so and produces nothing, because a report in the wrong format is not a
delivered report.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.application.report.content import build_slot_values
from app.application.report.period import DEFAULT_TIMEZONE, WeekWindow, resolve_week
from app.application.report.render import render_report
from app.application.report.template import parse_template
from app.application.report.template_source import (
    TemplateCandidate,
    resolve_template,
)
from app.capabilities.knowledge import _answer_evidence
from app.capabilities.person_scope import (
    SOURCE_MENTION,
    SOURCE_PRIVATE,
    SOURCE_SENT,
    PersonScopeUnavailable,
    collect_resource_ids,
    identity_names_by_platform,
    identity_ids_of,
    mention_anchors,
    private_conversation_ids_of,
    resolve_person,
    search_content,
)
from app.infrastructure.agent_attachments import DOCX_MIME, AgentAttachmentStore
from app.kernel.execution_context import current_execution_context
from app.kernel.models import CapabilityDescriptor

CAPABILITY_NAME = "report.weekly"

NO_TEMPLATE_MESSAGE = "没有搜到任何周报模板"
NO_PERSON_MESSAGE = "我这边没有关于这个人的资料，请确认名字后再试。"
MULTIPLE_PEOPLE_MESSAGE = "我找到了多个同名的人，请确认是哪一位。"
SELF_NAMES = {"我", "自己", "本人", "我的", "我自己", "我本人", "我的周报"}
SELF_REPORT_FILE_STEM = "我的周报"

_REPORT_FILE_NAME_PATTERNS = (
    re.compile(
        r"(?:文档|文件)(?:的)?(?:名字|名称|名)\s*[:：]?\s*"
        r"(?:叫|为|是|命名为|设为)?\s*"
        r"(?P<name>[^，。；;,\n\r]{1,160})"
    ),
    re.compile(
        r"(?:标题|题目)\s*[:：]?\s*"
        r"(?:叫|为|是|命名为|设为)?\s*"
        r"(?P<name>[^，。；;,\n\r]{1,160})"
    ),
    re.compile(
        r"周报(?:的)?(?:名字|名称|名)\s*[:：]?\s*"
        r"(?:叫|为|是)?\s*(?P<name>[^，。；;,\n\r]{1,160})"
    ),
    re.compile(
        r"(?:命名为|取名(?:为|叫|成)?|命名成)\s*"
        r"(?P<name>[^，。；;,\n\r]{1,160})"
    ),
)


def _is_self_report(person: str, subject: str) -> bool:
    typed = " ".join(str(person or "").split()).strip()
    return subject in SELF_NAMES or typed in SELF_NAMES


def _requested_file_name(instruction: str) -> str:
    """Read an explicitly named report document from the user's instruction."""

    text = " ".join(str(instruction or "").split()).strip()
    if not text:
        return ""
    for pattern in _REPORT_FILE_NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = match.group("name").strip().strip("「」『』“”\"'《》").strip()
        if name and name != SELF_REPORT_FILE_STEM and name.endswith("的周报"):
            name = name[: -len("的周报")].strip()
        if name:
            return name[:200]
    return ""


def _report_title(report_name: str) -> str:
    """The document heading. The person's name leads it, self report or not."""

    return f"{report_name}的周报"


def _title_from_file_name(file_name: str) -> str:
    stem = re.sub(r"\.docx$", "", str(file_name or "").strip(), flags=re.IGNORECASE)
    return stem.strip()


def _report_name(
    person: str,
    subject: str,
    display_name: str,
    context,
    match: dict[str, Any] | None = None,
) -> str:
    """The name the report is addressed to.

    Two rules. A report about somebody the user named uses the name the user
    wrote; a report about the user themselves is addressed with their own
    platform name, Feishu first, then WeChat, then the account nickname. The
    resolved identity display name is only the last fallback: for a self
    report it can be a generated account label ("wxid_...", "用户12345"),
    which is not a name anybody wants printed on the document.
    """

    typed = " ".join(str(person or "").split()).strip()
    if _is_self_report(typed, subject):
        names = identity_names_by_platform(match or {})
        for platform in ("feishu", "wechat"):
            for value in names.get(platform, ()):
                if str(value).strip():
                    return str(value).strip()
        owner_name = str((context.source_ref or {}).get("owner_name") or "").strip()
        return owner_name or display_name
    return typed or subject or display_name

# Broad, work-oriented query for the report's material: retrieval still runs
# inside the person's scoped resources, so the terms only decide ranking.
REPORT_QUERY = (
    "本周工作 完成 进展 计划 风险 问题 需求 对接 部署 开发 上线 联调 测试"
)


class WeeklyReportUnavailable(RuntimeError):
    classification = "retryable_error"


class WeeklyReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=80)
    instruction: str = Field(default="", max_length=2000)
    # The .docx uploaded with this turn (used when the user says "我上传的模板").
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    # A candidate the user picked from a previous "which template?" answer.
    template_attachment_id: str = Field(default="", max_length=80)
    file_name: str = Field(default="", max_length=200)
    time_range: str | None = Field(default=None, max_length=200)
    top_k: int = Field(default=30, ge=5, le=50)


class WeeklyReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str  # ready | no_template | no_person | needs_selection | needs_person_choice
    message: str
    title: str = ""
    subject_name: str = ""
    period: str = ""
    file_name: str = ""
    attachment_id: str = ""
    size_bytes: int = 0
    expires_at: str = ""
    template_origin: str = ""
    # Citation shape, so the card reuses the same 来源 list the answer uses and
    # so the list survives a reload through the stored observation.
    citations: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: int = 0


class WeeklyReportCapability:
    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description=(
            "给某个人写一份周报（只读资料 + 产出 .docx 文件）："
            "复用“他发的 / 我和他的私聊 / 提到他的”三类来源，"
            "按指定模板的格式生成 Word 文档。"
        ),
        input_schema=WeeklyReportInput.model_json_schema(),
        output_schema=WeeklyReportOutput.model_json_schema(),
        # The only write is the Agent's own 24h artifact bucket; nothing leaves
        # the system, so the plan's "生成不需要审批" holds.
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=180,
    )

    def __init__(
        self,
        rag_client,
        knowledge_client,
        provider,
        attachments: AgentAttachmentStore,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        clock=None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.rag = rag_client
        self.knowledge = knowledge_client
        self.provider = provider
        self.attachments = attachments
        self.timezone_name = timezone_name or DEFAULT_TIMEZONE
        self.clock = clock
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> WeeklyReportInput:
        return WeeklyReportInput.model_validate(arguments)

    def execute(self, arguments: WeeklyReportInput) -> dict[str, Any]:
        context = current_execution_context()
        now = (self.clock or (lambda: datetime.now(timezone.utc)))()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        window = resolve_week(
            arguments.time_range or arguments.instruction,
            now=now,
            timezone_name=self.timezone_name,
        )
        try:
            subject, matches = resolve_person(
                self.knowledge,
                owner_user_id=context.owner_user_id,
                name=arguments.person,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        except PersonScopeUnavailable as exc:
            raise WeeklyReportUnavailable("person lookup is unavailable") from exc
        if not matches:
            return WeeklyReportOutput(
                status="no_person",
                message=NO_PERSON_MESSAGE,
                period=window.period_text,
            ).model_dump()
        if len(matches) > 1:
            return WeeklyReportOutput(
                status="needs_person_choice",
                message=MULTIPLE_PEOPLE_MESSAGE,
                period=window.period_text,
                candidates=[
                    {
                        "person_key": item.get("person_key"),
                        "display_name": item.get("display_name"),
                    }
                    for item in matches
                ],
            ).model_dump()
        match = matches[0]
        display_name = str(match.get("display_name") or subject or arguments.person)
        is_self = _is_self_report(arguments.person, subject)
        report_name = _report_name(
            arguments.person, subject, display_name, context, match
        )
        requested_file_name = (
            str(arguments.file_name or "").strip()
            or _requested_file_name(arguments.instruction)
        )
        default_title = _report_title(report_name)
        document_title = (
            _title_from_file_name(requested_file_name) or default_title
            if requested_file_name
            else default_title
        )

        template = self._resolve_template(arguments, context)
        if template.status == "not_found":
            return WeeklyReportOutput(
                status="no_template",
                message=NO_TEMPLATE_MESSAGE,
                title=document_title,
                subject_name=report_name,
                period=window.period_text,
            ).model_dump()
        if template.status == "needs_selection":
            return WeeklyReportOutput(
                status="needs_selection",
                message="找到多份周报模板，请选择要使用哪一份。",
                title=document_title,
                subject_name=report_name,
                period=window.period_text,
                file_name=requested_file_name,
                candidates=[
                    {
                        **candidate.to_dict(),
                        **_materialize_candidate(
                            candidate, context, self.attachments, self.knowledge
                        ),
                    }
                    for candidate in template.candidates
                ],
            ).model_dump()

        chosen = template.chosen
        try:
            template_bytes = self._read_template(chosen, context)
        except Exception:  # noqa: BLE001 - report a readable failure instead
            # The template was located but its bytes are gone (a pending
            # upload, an expired object). Falling back to a default layout
            # would silently hand back a document in the wrong format.
            return WeeklyReportOutput(
                status="no_template",
                message=NO_TEMPLATE_MESSAGE,
                title=document_title,
                subject_name=report_name,
                period=window.period_text,
            ).model_dump()
        spec = parse_template(template_bytes)
        # "提到他的消息" is anchored on the name others would write. A
        # self-reference is the pronoun 我, which matches almost every sentence,
        # so the owner's own platform names anchor that window instead -- one
        # query per name, because content_contains is an AND.
        mention_anchor = (
            mention_anchors(match) if subject in SELF_NAMES else (subject,)
        )
        evidence = self._evidence(
            arguments, context, match, subject, mention_anchor, window
        )
        draft = self.provider.generate(
            person=report_name,
            period=window.period_text,
            time_range=window.range_text,
            template=spec.prompt_payload(),
            evidence=evidence,
            conversation_context=context.conversation_context,
        )
        values = build_slot_values(
            spec=spec,
            draft_values=draft.values,
            person_name=report_name,
            window=window,
        )
        rendered = render_report(template_bytes, values)
        file_name = (
            requested_file_name
            or window.file_name(report_name)
        )
        if not file_name.lower().endswith(".docx"):
            file_name = f"{file_name}.docx"
        artifact = self.attachments.save(
            owner_id=context.owner_user_id,
            file_name=file_name,
            mime_type=DOCX_MIME,
            data=rendered,
        )
        return WeeklyReportOutput(
            status="ready",
            message=f"已按模板生成 {report_name} 的周报（{window.label}）。",
            title=document_title,
            subject_name=report_name,
            period=window.period_text,
            file_name=artifact["file_name"],
            attachment_id=artifact["attachment_id"],
            size_bytes=artifact["size_bytes"],
            expires_at=artifact["expires_at"],
            template_origin=chosen.origin,
            citations=_citations(evidence),
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()

    # -- helpers ----------------------------------------------------------

    def _resolve_template(self, arguments: WeeklyReportInput, context):
        if arguments.template_attachment_id:
            record = self.attachments.metadata(arguments.template_attachment_id)
            if record:
                return _resolved_from_upload(record)
        uploaded = [
            record
            for record in (
                self.attachments.metadata(attachment_id)
                for attachment_id in arguments.attachment_ids
            )
            if record
        ]
        return resolve_template(
            instruction=arguments.instruction,
            uploaded=uploaded,
            knowledge=self.knowledge,
            owner_user_id=context.owner_user_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )

    def _read_template(self, candidate: TemplateCandidate, context) -> bytes:
        if candidate.origin == "uploaded":
            return self.attachments.read(candidate.attachment_id)
        return self.knowledge.open_attachment(
            owner_user_id=context.owner_user_id,
            attachment_id=candidate.attachment_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )

    def _evidence(
        self,
        arguments: WeeklyReportInput,
        context,
        match: dict[str, Any],
        subject: str,
        anchors: tuple[str, ...],
        window: WeekWindow,
    ) -> list[dict[str, Any]]:
        identity_ids = identity_ids_of(match)
        private_ids = private_conversation_ids_of(match)
        resource_ids = list(
            collect_resource_ids(
                self.rag,
                owner_user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                identity_ids=identity_ids,
                private_ids=private_ids,
                subject=subject,
                source_scope=(SOURCE_SENT, SOURCE_PRIVATE),
                top_k=arguments.top_k,
                occurred_after=window.start_iso,
                occurred_before=window.end_iso,
            )
        )
        for anchor in anchors:
            resource_ids.extend(
                collect_resource_ids(
                    self.rag,
                    owner_user_id=context.owner_user_id,
                    organization_id=context.organization_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    identity_ids=identity_ids,
                    private_ids=private_ids,
                    subject=anchor,
                    source_scope=(SOURCE_MENTION,),
                    top_k=arguments.top_k,
                    occurred_after=window.start_iso,
                    occurred_before=window.end_iso,
                )
            )
        resource_ids = list(dict.fromkeys(resource_ids))
        if not resource_ids:
            return []
        results = search_content(
            self.rag,
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            resource_ids=resource_ids,
            question=REPORT_QUERY,
            name=arguments.person,
            subject=subject,
            top_k=arguments.top_k,
        )
        return _answer_evidence(results, self.timezone_name)


def _resolved_from_upload(record: dict[str, Any]):
    from app.application.report.template_source import TemplateResolution

    candidate = TemplateCandidate(
        origin="uploaded",
        attachment_id=str(record.get("attachment_id") or ""),
        file_name=str(record.get("file_name") or "模板.docx"),
        size_bytes=int(record.get("size_bytes") or 0),
        created_at=str(record.get("uploaded_at") or ""),
        object_name=str(record.get("minio_object_name") or ""),
    )
    return TemplateResolution(
        status="resolved", candidates=[candidate], chosen=candidate
    )


def _materialize_candidate(
    candidate: TemplateCandidate,
    context,
    attachments: AgentAttachmentStore,
    knowledge,
) -> dict[str, Any]:
    """Copy a collected template into the Agent store so it can be previewed.

    The preview endpoint only serves the Agent's own bucket, and the copy also
    gives the selection a stable id the next turn can point at. A failed copy
    leaves the candidate selectable by its collected id.
    """

    try:
        data = knowledge.open_attachment(
            owner_user_id=context.owner_user_id,
            attachment_id=candidate.attachment_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )
        saved = attachments.save(
            owner_id=context.owner_user_id,
            file_name=candidate.file_name,
            mime_type=DOCX_MIME,
            data=data,
        )
    except Exception:  # noqa: BLE001 - the candidate list must still render
        return {"preview_attachment_id": ""}
    return {"preview_attachment_id": saved["attachment_id"]}


def _citations(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The traceability list shown beside the result, never inside the .docx.

    Retrieval returns documents as several chunks, so a raw chunk list shows the
    same file three or four times. A document is one source per conversation it
    was sent in (send 《公司简介》 to two chats and both are listed, but each
    once), and a message is one source however many chunks it was split into.
    The entries keep the citation shape answer.compose already produces so the
    result card can reuse the same source list the Q&A answer uses.
    """

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence or []:
        resource_type = str(item.get("resource_type") or "")
        resource_id = str(item.get("resource_id") or "")
        conversation_id = str(item.get("conversation_id") or "")
        if resource_type == "attachment":
            # The file name identifies the document; the conversation identifies
            # which copy was sent. Falls back to the attachment id for a file
            # whose name is missing, so unnamed attachments stay separate.
            document = str(item.get("title") or "").strip() or resource_id
            key = ("document", conversation_id, document)
        else:
            key = ("message", resource_id, "")
        if not any(key) or key in seen:
            continue
        seen.add(key)
        quote = str(item.get("quote") or "")
        # A collected message can be a provider envelope (WeChat emoji/XML).
        # The source stays traceable through its conversation and time, but the
        # raw payload must not be shown as if it were readable text.
        if quote.lstrip().startswith("<") or "<msg" in quote:
            quote = ""
        out.append(
            {
                "evidence_id": str(item.get("evidence_id") or resource_id),
                "quote": quote[:500],
                "title": item.get("title") or "",
                "resource_id": resource_id,
                "resource_type": resource_type,
                "conversation_id": item.get("conversation_id") or None,
                "conversation_name": item.get("conversation_name") or "",
                "sender_name": item.get("sender_name") or "",
                "sent_at": item.get("sent_at") or "",
                "sent_at_local": item.get("sent_at_local") or "",
                "position": item.get("position"),
            }
        )
        if len(out) >= 30:
            break
    return out
