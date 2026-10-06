from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.rag.client import RAGClient
from app.kernel.execution_context import current_execution_context
from app.kernel.models import (
    CapabilityDescriptor,
    CapabilityInputBinding,
    StepOutputRef,
)

SEARCH_SOURCES_NAME = "knowledge.search_sources"
SEARCH_CONTENT_NAME = "knowledge.search_content"
KNOWLEDGE_ANSWER_NAME = "knowledge.answer"

MAX_ANSWER_CHUNKS = 50
QUOTE_CHARS = 500

ATTACHMENT_EXTENSIONS = {
    "pdf": ("pdf",),
    "excel": ("xlsx", "xls", "csv"),
    "word": ("docx", "doc"),
    "ppt": ("pptx", "ppt"),
    "image": ("png", "jpg", "jpeg", "gif", "webp", "bmp"),
}


class KnowledgeToolUnavailable(RuntimeError):
    pass


class SearchSourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, max_length=200)
    sender_names: list[str] = Field(default_factory=list, max_length=10)
    sender_ids: list[str] = Field(default_factory=list, max_length=10)
    conversation_names: list[str] = Field(default_factory=list, max_length=10)
    conversation_ids: list[str] = Field(default_factory=list, max_length=10)
    occurred_after: str | None = None
    occurred_before: str | None = None
    attachment_types: list[str] = Field(default_factory=list, max_length=5)
    message_types: list[str] = Field(default_factory=list, max_length=5)
    resource_types: list[str] = Field(
        default_factory=lambda: ["message", "attachment"],
        max_length=2,
    )
    include_personal: bool = False
    top_k: int = Field(default=20, ge=1, le=50)


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str
    resource_type: str
    knowledge_item_id: str | None = None
    title: str | None = None
    file_name: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    sender_platform: str | None = None
    conversation_id: str | None = None
    conversation_name: str | None = None
    conversation_type: str | None = None
    conversation_platform: str | None = None
    sent_at: str | None = None
    preview: str | None = None
    score: float = 0
    score_type: str = "rrf"


class SearchSourcesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceInfo]
    resource_ids: list[str]
    returned_count: int
    has_more: bool
    summary: str
    metadata_coverage: str


class SearchContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    resource_ids: list[str] = Field(default_factory=list, max_length=20)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=5)
    include_personal: bool = False
    restrict_to_resource_ids: bool = False
    top_k: int = Field(default=10, ge=1, le=50)


class SearchContentPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    resource_ids_ref: StepOutputRef | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=5)
    include_personal: bool = False
    restrict_to_resource_ids: bool = False
    top_k: int = Field(default=10, ge=1, le=50)


class ContentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    text: str
    score: float
    score_type: str = "rrf"
    position: dict[str, Any] | None = None


class ContentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str
    resource_type: str
    title: str | None = None
    sender_name: str | None = None
    conversation_id: str | None = None
    conversation_name: str | None = None
    conversation_type: str | None = None
    conversation_platform: str | None = None
    sent_at: str | None = None
    best_score: float
    score_type: str = "rrf"
    matched_chunk_count: int
    chunks: list[ContentChunk]


class SearchContentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ContentResult]
    # The same chunks in the common evidence shape consumed by answer.compose.
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    returned_source_count: int
    returned_chunk_count: int
    has_more: bool
    summary: str
    metadata_coverage: str


class KnowledgeAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    results: list[ContentResult] = Field(default_factory=list, max_length=50)
    # Metadata questions ("which platform is this group on") are answered from
    # the source records, which carry fields the content endpoint does not.
    sources: list[SourceInfo] = Field(default_factory=list, max_length=50)
    metadata_coverage: str = "complete"
    # The window the retrieval already applied ("昨天" resolved by the router),
    # so the answer does not have to recompute relative dates itself.
    time_range: str | None = Field(default=None, max_length=200)
    # The user's timezone, so evidence timestamps can be rendered locally
    # instead of leaving the model to convert UTC in its head.
    timezone: str | None = Field(default=None, max_length=64)


class KnowledgeAnswerPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    time_range: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    results_ref: StepOutputRef | None = Field(
        default=None,
        description="引用更早 knowledge.search_content 步骤输出的 results",
    )
    sources_ref: StepOutputRef | None = Field(
        default=None,
        description="引用更早 knowledge.search_sources 步骤输出的 sources",
    )
    metadata_coverage_ref: StepOutputRef | None = Field(
        default=None,
        description="引用 knowledge.search_content 步骤输出的 metadata_coverage",
    )


class KnowledgeAnswerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[dict[str, Any]]
    metadata_coverage: str = "complete"
    model_calls: int = 0


class KnowledgeSearchSourcesCapability:
    descriptor = CapabilityDescriptor(
        name=SEARCH_SOURCES_NAME,
        description=(
            "根据发送人、群聊、时间和资源类型定位知识来源（只读）。"
            "适合回答谁发过什么、在哪个群、什么时间发送。"
        ),
        input_schema=SearchSourcesInput.model_json_schema(),
        output_schema=SearchSourcesOutput.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=30,
    )

    def __init__(self, client: RAGClient) -> None:
        self.client = client

    def validate(self, arguments: dict[str, Any]) -> SearchSourcesInput:
        return SearchSourcesInput.model_validate(arguments)

    def execute(self, arguments: SearchSourcesInput) -> dict[str, Any]:
        context = current_execution_context()
        requests = _scope_requests(
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            include_personal=arguments.include_personal,
        )
        body = {
            "query": arguments.query or "",
            "sender_ids": arguments.sender_ids,
            "sender_names": arguments.sender_names,
            "conversation_ids": arguments.conversation_ids,
            "conversation_names": arguments.conversation_names,
            "resource_types": arguments.resource_types,
            "file_extensions": _attachment_extensions(arguments.attachment_types),
            "message_types": arguments.message_types,
            "occurred_after": arguments.occurred_after,
            "occurred_before": arguments.occurred_before,
            "top_k": arguments.top_k,
            "include_protected": True,
        }
        responses = _parallel_scope_calls(
            requests,
            lambda scope: self.client.search_sources(
                {**body, **scope},
                user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )
        merged = _merge_sources(responses, limit=arguments.top_k)
        output = SearchSourcesOutput(
            sources=merged,
            resource_ids=[item.resource_id for item in merged],
            returned_count=len(merged),
            has_more=any(bool(item.get("has_more")) for item in responses),
            summary=_sources_summary(merged, arguments),
            metadata_coverage=_coverage(responses),
        )
        return output.model_dump()


class KnowledgeSearchContentCapability:
    descriptor = CapabilityDescriptor(
        name=SEARCH_CONTENT_NAME,
        description=(
            "检索知识库中的具体内容片段（只读）。"
            "适合回答文件或消息里写了什么，可限制在 search_sources 返回的资源内。"
        ),
        input_schema=SearchContentInput.model_json_schema(),
        planner_input_schema=SearchContentPlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="resource_ids_ref",
                runtime_argument="resource_ids",
                source_capability=SEARCH_SOURCES_NAME,
                source_output="resource_ids",
            )
        ],
        output_schema=SearchContentOutput.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=30,
    )

    def __init__(self, client: RAGClient, *, min_answer_score: float = 0.0) -> None:
        self.client = client
        # Retrieval scores are RRF values, which have no absolute meaning across
        # deployments. The default keeps every hit; a deployment that sees
        # unrelated chunks survive retrieval raises this after sampling its own
        # score distribution.
        self.min_answer_score = max(float(min_answer_score), 0.0)

    def validate(self, arguments: dict[str, Any]) -> SearchContentInput:
        return SearchContentInput.model_validate(arguments)

    def execute(self, arguments: SearchContentInput) -> dict[str, Any]:
        context = current_execution_context()
        if arguments.restrict_to_resource_ids and not arguments.resource_ids:
            return SearchContentOutput(
                results=[],
                returned_source_count=0,
                returned_chunk_count=0,
                has_more=False,
                summary="未找到可继续检索的来源",
                metadata_coverage="complete",
            ).model_dump()
        requests = _scope_requests(
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            include_personal=arguments.include_personal,
        )
        body = {
            "query": arguments.query,
            "resource_ids": arguments.resource_ids,
            "knowledge_base_ids": arguments.knowledge_base_ids,
            "top_k": arguments.top_k,
            "include_protected": True,
            "group_by_source": True,
        }
        responses = _parallel_scope_calls(
            requests,
            lambda scope: self.client.search_content(
                {**body, **scope},
                user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )
        merged = _merge_content(
            responses,
            limit=arguments.top_k,
            min_score=self.min_answer_score,
        )
        output = SearchContentOutput(
            results=merged,
            evidence=_answer_evidence(merged),
            returned_source_count=len(merged),
            returned_chunk_count=sum(len(item.chunks) for item in merged),
            has_more=any(bool(item.get("has_more")) for item in responses),
            summary=_content_summary(merged, arguments),
            metadata_coverage=_coverage(responses),
        )
        return output.model_dump()


class KnowledgeAnswerCapability:
    descriptor = CapabilityDescriptor(
        name=KNOWLEDGE_ANSWER_NAME,
        description=(
            "依据 knowledge.search_content 返回的 chunks 生成带引用的回答（只读）。"
            "无检索结果时不调用模型，返回明确的空结果。"
        ),
        input_schema=KnowledgeAnswerInput.model_json_schema(),
        planner_input_schema=KnowledgeAnswerPlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="results_ref",
                runtime_argument="results",
                source_capability=SEARCH_CONTENT_NAME,
                source_output="results",
            ),
            CapabilityInputBinding(
                planner_argument="metadata_coverage_ref",
                runtime_argument="metadata_coverage",
                source_capability=SEARCH_CONTENT_NAME,
                source_output="metadata_coverage",
            ),
        ],
        output_schema=KnowledgeAnswerOutput.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=60,
    )

    def __init__(self, provider, *, timeout_seconds: int | None = None) -> None:
        self.provider = provider
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> KnowledgeAnswerInput:
        return KnowledgeAnswerInput.model_validate(arguments)

    def execute(self, arguments: KnowledgeAnswerInput) -> dict[str, Any]:
        evidence = _answer_evidence(arguments.results, arguments.timezone)
        if not evidence:
            evidence = _source_evidence(arguments.sources, arguments.timezone)
        if not evidence:
            return KnowledgeAnswerOutput(
                answer="没有找到满足条件的内容。",
                citations=[],
                metadata_coverage=arguments.metadata_coverage,
                model_calls=0,
            ).model_dump()

        draft = (
            self.provider.compose(
                arguments.query, evidence, time_range=arguments.time_range
            )
            if arguments.time_range
            else self.provider.compose(arguments.query, evidence)
        )
        return KnowledgeAnswerOutput(
            answer=str(draft.answer),
            citations=_known_knowledge_citations(draft.citations, evidence),
            metadata_coverage=arguments.metadata_coverage,
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()


def _scope_requests(
    *,
    owner_user_id: str,
    organization_id: str | None,
    include_personal: bool,
) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    if organization_id:
        requests.append(
            {
                "scope_type": "organization",
                "scope_id": organization_id,
            }
        )
    elif not include_personal:
        raise KnowledgeToolUnavailable("current organization is unavailable")
    if include_personal:
        requests.append({"scope_type": "user", "scope_id": owner_user_id})
    return requests


def _parallel_scope_calls(
    scopes: list[dict[str, str]],
    call,
) -> list[dict[str, Any]]:
    if len(scopes) == 1:
        return [call(scopes[0])]
    with ThreadPoolExecutor(max_workers=len(scopes)) as pool:
        futures = [pool.submit(call, scope) for scope in scopes]
        return [future.result() for future in futures]


def _merge_sources(
    responses: list[dict[str, Any]],
    *,
    limit: int,
) -> list[SourceInfo]:
    selected: dict[str, SourceInfo] = {}
    for response in responses:
        for raw in response.get("items") or []:
            if not isinstance(raw, dict):
                continue
            item = SourceInfo(
                resource_id=str(raw.get("resource_id") or ""),
                resource_type=str(raw.get("resource_type") or ""),
                knowledge_item_id=_text(raw.get("knowledge_item_id")),
                title=_text(raw.get("title")),
                file_name=_text(raw.get("file_name")),
                sender_id=_text((raw.get("sender") or {}).get("id")),
                sender_name=_text((raw.get("sender") or {}).get("name")),
                sender_platform=_text((raw.get("sender") or {}).get("platform")),
                conversation_id=_text((raw.get("conversation") or {}).get("id")),
                conversation_name=_text((raw.get("conversation") or {}).get("name")),
                conversation_type=_text((raw.get("conversation") or {}).get("type")),
                conversation_platform=_text(
                    (raw.get("conversation") or {}).get("platform")
                ),
                sent_at=_text(raw.get("sent_at")),
                preview=_text(raw.get("preview")),
                score=float(raw.get("score") or 0),
                score_type=str(raw.get("score_type") or "rrf"),
            )
            if not item.resource_id:
                continue
            key = f"{item.resource_type}:{item.resource_id}"
            current = selected.get(key)
            if current is None or item.score > current.score:
                selected[key] = item
    return sorted(selected.values(), key=lambda item: item.score, reverse=True)[:limit]


def _merge_content(
    responses: list[dict[str, Any]],
    *,
    limit: int,
    min_score: float = 0.0,
) -> list[ContentResult]:
    grouped: dict[str, dict[str, Any]] = {}
    for response in responses:
        for raw in response.get("items") or []:
            if not isinstance(raw, dict):
                continue
            resource_id = str(raw.get("resource_id") or "")
            resource_type = str(raw.get("resource_type") or "")
            if not resource_id:
                continue
            key = f"{resource_type}:{resource_id}"
            item = grouped.setdefault(
                key,
                {
                    "resource_id": resource_id,
                    "resource_type": resource_type,
                    "title": _text(raw.get("title")),
                    "sender_name": _text((raw.get("sender") or {}).get("name")),
                    "conversation_id": _text(
                        (raw.get("conversation") or {}).get("id")
                    ),
                    "conversation_name": _text(
                        (raw.get("conversation") or {}).get("name")
                    ),
                    "conversation_type": _text(
                        (raw.get("conversation") or {}).get("type")
                    ),
                    "conversation_platform": _text(
                        (raw.get("conversation") or {}).get("platform")
                    ),
                    "sent_at": _text(raw.get("sent_at")),
                    "best_score": float(raw.get("best_score") or 0),
                    "chunks": {},
                },
            )
            item["best_score"] = max(
                float(item["best_score"]),
                float(raw.get("best_score") or 0),
            )
            for chunk in raw.get("chunks") or []:
                if not isinstance(chunk, dict):
                    continue
                chunk_id = str(chunk.get("chunk_id") or "")
                if not chunk_id:
                    continue
                # A chunk below the relevance floor never reaches the answer
                # layer, so an unrelated retrieval cannot be quoted at all.
                if float(chunk.get("score") or 0) < min_score:
                    continue
                item["chunks"][chunk_id] = {
                    "chunk_id": chunk_id,
                    "text": str(chunk.get("text") or ""),
                    "score": float(chunk.get("score") or 0),
                    "score_type": str(chunk.get("score_type") or "rrf"),
                    "position": chunk.get("position"),
                }
    results: list[ContentResult] = []
    for item in grouped.values():
        chunks = sorted(
            item.pop("chunks").values(),
            key=lambda chunk: chunk["score"],
            reverse=True,
        )[:3]
        if not chunks:
            # Every chunk of this resource was filtered out by the floor.
            continue
        results.append(
            ContentResult(
                **item,
                score_type="rrf",
                matched_chunk_count=len(chunks),
                chunks=[ContentChunk(**chunk) for chunk in chunks],
            )
        )
    return sorted(results, key=lambda item: item.best_score, reverse=True)[:limit]


def _attachment_extensions(values: list[str]) -> list[str]:
    extensions: list[str] = []
    for value in values:
        extensions.extend(ATTACHMENT_EXTENSIONS.get(str(value).lower(), ()))
    return list(dict.fromkeys(extensions))


def _coverage(responses: list[dict[str, Any]]) -> str:
    values = {
        str((response.get("diagnostics") or {}).get("metadata_coverage") or "")
        for response in responses
    }
    return "partial" if "partial" in values else "complete"


def _sources_summary(sources: list[SourceInfo], arguments: SearchSourcesInput) -> str:
    if not sources:
        return "未找到匹配的知识来源"
    conditions: list[str] = []
    if arguments.sender_names:
        conditions.append(f"发送人={','.join(arguments.sender_names)}")
    if arguments.conversation_names:
        conditions.append(f"群聊={','.join(arguments.conversation_names)}")
    if arguments.occurred_after or arguments.occurred_before:
        conditions.append("指定时间范围")
    suffix = f"，条件：{'、'.join(conditions)}" if conditions else ""
    return f"找到{len(sources)}个知识来源{suffix}"


def _content_summary(results: list[ContentResult], arguments: SearchContentInput) -> str:
    if not results:
        return f"未找到与“{arguments.query}”相关的内容"
    return f"在{len(results)}个资源中找到与“{arguments.query}”相关的内容"


def _answer_evidence(
    results: list[ContentResult],
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for result in results:
        for chunk in result.chunks:
            if len(evidence) >= MAX_ANSWER_CHUNKS:
                return evidence
            quote = str(chunk.text or "").strip()[:QUOTE_CHARS]
            if not quote:
                continue
            evidence.append(
                {
                    "evidence_id": chunk.chunk_id,
                    # Tells answer.compose which source this came from, so the
                    # answer can attribute a fact to the knowledge base rather
                    # than to an uploaded attachment.
                    "fetch_method": "knowledge",
                    "resource_id": result.resource_id,
                    "resource_type": result.resource_type,
                    "title": result.title,
                    "sender_name": result.sender_name,
                    "conversation_id": result.conversation_id,
                    "conversation_name": result.conversation_name,
                    "conversation_type": result.conversation_type,
                    "conversation_platform": result.conversation_platform,
                    "sent_at": result.sent_at,
                    "sent_at_local": _local_time(result.sent_at, timezone_name),
                    "position": chunk.position,
                    "quote": quote,
                    "snippet": quote,
                }
            )
    return evidence


def _source_evidence(
    sources: list[SourceInfo],
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    """One evidence entry per source record, rendered as readable metadata.

    The source lookup carries fields the content endpoint does not (the
    conversation's platform, for instance), so a metadata question is answered
    from these records rather than from message chunks.
    """

    evidence: list[dict[str, Any]] = []
    for source in sources:
        if len(evidence) >= MAX_ANSWER_CHUNKS:
            break
        local = _local_time(source.sent_at, timezone_name)
        facts: list[str] = []
        if source.conversation_name:
            facts.append(f"群聊={source.conversation_name}")
        if source.conversation_platform:
            facts.append(f"平台={source.conversation_platform}")
        if source.conversation_type:
            facts.append(f"会话类型={source.conversation_type}")
        if source.sender_name:
            facts.append(f"发送人={source.sender_name}")
        if local:
            facts.append(f"发送时间={local}")
        if source.file_name:
            facts.append(f"文件名={source.file_name}")
        if source.title:
            facts.append(f"标题={source.title}")
        if source.preview:
            facts.append(f"摘要={source.preview[:200]}")
        quote = "；".join(facts) or source.resource_id
        evidence.append(
            {
                "evidence_id": f"source:{source.resource_id}",
                "resource_id": source.resource_id,
                "resource_type": source.resource_type,
                "title": source.title,
                "file_name": source.file_name,
                "sender_name": source.sender_name,
                "sender_platform": source.sender_platform,
                "conversation_id": source.conversation_id,
                "conversation_name": source.conversation_name,
                "conversation_type": source.conversation_type,
                "conversation_platform": source.conversation_platform,
                "sent_at": source.sent_at,
                "sent_at_local": local,
                "quote": quote,
                "snippet": quote,
            }
        )
    return evidence


def _local_time(value: str | None, timezone_name: str | None) -> str | None:
    """Render a UTC timestamp in the user's timezone for the answer model."""

    if not value or not timezone_name:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _known_knowledge_citations(
    citations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {
        str(item.get("evidence_id")): item
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in citations or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        source = known.get(evidence_id)
        if not evidence_id or source is None or evidence_id in seen:
            continue
        seen.add(evidence_id)
        kept.append(
            {
                "evidence_id": evidence_id,
                "quote": str(item.get("quote") or source.get("quote") or "")[
                    :QUOTE_CHARS
                ],
                "resource_id": source.get("resource_id"),
                "resource_type": source.get("resource_type"),
                "title": source.get("title"),
                "sender_name": source.get("sender_name"),
                "conversation_id": source.get("conversation_id"),
                "conversation_name": source.get("conversation_name"),
                "conversation_type": source.get("conversation_type"),
                "conversation_platform": source.get("conversation_platform"),
                "sent_at": source.get("sent_at"),
                "position": source.get("position"),
            }
        )
    return kept


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
