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
SEARCH_TREE_NAME = "knowledge.search_tree"
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
    # A phrase that must appear in the resource body. Used to keep a subject
    # lookup honest: BM25 matches "深空公司" and "公司" alike, so a caller that
    # needs the full subject name adds it here and the index enforces it.
    content_contains: list[str] = Field(default_factory=list, max_length=5)
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
    # Paging for callers that need the whole set ("everything he ever sent")
    # rather than the newest page. The index sorts newest-first when the query
    # is empty, so offset walks backwards through time deterministically.
    offset: int = Field(default=0, ge=0, le=100000)


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
    # The RAG service accepts 100 per call; a wider scope is chunked by the
    # caller instead of being silently dropped here.
    resource_ids: list[str] = Field(default_factory=list, max_length=100)
    # A person's scope can also be expressed as filters, which is what lets one
    # query cover the whole window instead of enumerating every resource id:
    # what he sent, the chats he is part of, and what mentions him.
    sender_ids: list[str] = Field(default_factory=list, max_length=20)
    conversation_ids: list[str] = Field(default_factory=list, max_length=20)
    content_contains: list[str] = Field(default_factory=list, max_length=10)
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


class SearchTreeInput(SearchContentInput):
    fallback_to_content: bool = True


class SearchTreePlanInput(SearchContentPlanInput):
    fallback_to_content: bool = True


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


class SearchTreeOutput(SearchContentOutput):
    retrieval_path: str = "tree"
    tree_mode: str = "tree"
    fallback_reason: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    locate_ms: float = 0.0


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
    # A caller that scoped the retrieval to a subject ("深空公司") knows why
    # the evidence is empty; saying so beats the generic empty-result line.
    empty_answer: str | None = Field(default=None, max_length=300)


class KnowledgeAnswerPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    time_range: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    results_ref: StepOutputRef | None = Field(
        default=None,
        description=(
            "引用更早 knowledge.search_content 或 knowledge.search_tree "
            "步骤输出的 results"
        ),
    )
    sources_ref: StepOutputRef | None = Field(
        default=None,
        description="引用更早 knowledge.search_sources 步骤输出的 sources",
    )
    empty_answer: str | None = Field(
        default=None,
        description="检索结果为空时直接返回的说明，例如“没有找到深空公司的资料”。",
        max_length=300,
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
            "content_contains": arguments.content_contains,
            "resource_types": arguments.resource_types,
            "file_extensions": _attachment_extensions(arguments.attachment_types),
            "message_types": arguments.message_types,
            "occurred_after": arguments.occurred_after,
            "occurred_before": arguments.occurred_before,
            "top_k": arguments.top_k,
            "offset": arguments.offset,
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
            "sender_ids": arguments.sender_ids,
            "conversation_ids": arguments.conversation_ids,
            "content_contains": arguments.content_contains,
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


class KnowledgeSearchTreeCapability:
    descriptor = CapabilityDescriptor(
        name=SEARCH_TREE_NAME,
        description=(
            "按实体范围检索知识库中的具体内容片段（只读）。"
            "适合已经出现项目、组织或人名的问题；树没有命中时可按参数回退传统检索。"
        ),
        input_schema=SearchTreeInput.model_json_schema(),
        planner_input_schema=SearchTreePlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="resource_ids_ref",
                runtime_argument="resource_ids",
                source_capability=SEARCH_SOURCES_NAME,
                source_output="resource_ids",
            )
        ],
        output_schema=SearchTreeOutput.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=30,
    )

    def __init__(
        self,
        client: RAGClient,
        content_capability: KnowledgeSearchContentCapability,
        *,
        min_answer_score: float = 0.0,
    ) -> None:
        self.client = client
        self.content_capability = content_capability
        self.min_answer_score = max(float(min_answer_score), 0.0)

    def validate(self, arguments: dict[str, Any]) -> SearchTreeInput:
        return SearchTreeInput.model_validate(arguments)

    def execute(self, arguments: SearchTreeInput) -> dict[str, Any]:
        context = current_execution_context()
        requests = _scope_requests(
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            include_personal=arguments.include_personal,
        )
        body = {
            "query": arguments.query,
            "resource_ids": arguments.resource_ids,
            "sender_ids": arguments.sender_ids,
            "conversation_ids": arguments.conversation_ids,
            "content_contains": arguments.content_contains,
            "knowledge_base_ids": arguments.knowledge_base_ids,
            "top_k": arguments.top_k,
            "include_protected": True,
        }
        responses = _parallel_scope_calls(
            requests,
            lambda scope: self.client.search_tree(
                {**body, **scope},
                user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )
        merged = _merge_tree_content(
            responses,
            limit=arguments.top_k,
            min_score=self.min_answer_score,
        )
        tree = _tree_diagnostics(responses)
        if merged or not arguments.fallback_to_content:
            return _tree_output(
                merged,
                responses=responses,
                diagnostics=tree,
                retrieval_path="tree",
                summary=_content_summary(merged, arguments),
            ).model_dump()

        fallback_arguments = SearchContentInput.model_validate(
            arguments.model_dump(exclude={"fallback_to_content"})
        )
        fallback = self.content_capability.execute(fallback_arguments)
        return _tree_output(
            [ContentResult.model_validate(item) for item in fallback["results"]],
            responses=responses,
            diagnostics=tree,
            retrieval_path="traditional_fallback",
            has_more=bool(fallback.get("has_more")),
            metadata_coverage=str(
                fallback.get("metadata_coverage") or tree["metadata_coverage"]
            ),
            summary=(
                f"树检索未命中（{tree.get('fallback_reason') or 'empty_scope'}），"
                "已回退传统检索"
            ),
        ).model_dump()


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
                source_capabilities=[SEARCH_CONTENT_NAME, SEARCH_TREE_NAME],
                source_output="results",
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
                answer=arguments.empty_answer or "没有找到满足条件的内容。",
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

    def execute_streaming(
        self,
        arguments: KnowledgeAnswerInput,
        *,
        sink,
        should_cancel=None,
    ) -> dict[str, Any]:
        evidence = _answer_evidence(arguments.results, arguments.timezone)
        if not evidence:
            evidence = _source_evidence(arguments.sources, arguments.timezone)
        if not evidence:
            answer = arguments.empty_answer or "没有找到满足条件的内容。"
            sink.push(answer)
            sink.complete(answer=answer, citations=[], warnings=[])
            return KnowledgeAnswerOutput(
                answer=answer,
                citations=[],
                metadata_coverage=arguments.metadata_coverage,
                model_calls=0,
            ).model_dump()

        stream_compose = getattr(self.provider, "stream_compose", None)
        if stream_compose is None:
            result = self.execute(arguments)
            sink.push(str(result.get("answer") or ""))
            sink.complete(
                answer=str(result.get("answer") or ""),
                citations=list(result.get("citations") or []),
                warnings=[],
            )
            return result

        kwargs: dict[str, Any] = {
            "on_delta": sink.push,
            "should_cancel": should_cancel,
        }
        if arguments.time_range:
            kwargs["time_range"] = arguments.time_range
        draft = stream_compose(arguments.query, evidence, **kwargs)
        citations = _known_knowledge_citations(draft.citations, evidence)
        warnings: list[str] = []
        if draft.citations and not citations:
            warnings.append("citation_selection_failed")
        sink.complete(
            answer=str(draft.answer),
            citations=citations,
            warnings=warnings,
        )
        return KnowledgeAnswerOutput(
            answer=str(draft.answer),
            citations=citations,
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


def _merge_tree_content(
    responses: list[dict[str, Any]],
    *,
    limit: int,
    min_score: float = 0.0,
) -> list[ContentResult]:
    """Adapt the tree endpoint's legacy chunk rows to the content contract."""
    grouped: dict[str, dict[str, Any]] = {}
    for response in responses:
        for raw in response.get("items") or []:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            resource_id = _text(raw.get("resource_id") or source.get("resource_id"))
            resource_type = _text(
                raw.get("resource_type") or source.get("resource_type")
            )
            chunk_id = _text(raw.get("chunk_id"))
            score = float(raw.get("best_score") or raw.get("score") or 0)
            if not resource_id or not chunk_id or score < min_score:
                continue
            key = ":".join(
                (
                    resource_type,
                    resource_id,
                    str(raw.get("content_version") or ""),
                )
            )
            sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
            conversation = (
                raw.get("conversation")
                if isinstance(raw.get("conversation"), dict)
                else {}
            )
            header = (
                raw.get("context_header")
                if isinstance(raw.get("context_header"), dict)
                else {}
            )
            item = grouped.setdefault(
                key,
                {
                    "resource_id": resource_id,
                    "resource_type": resource_type,
                    "title": _text(
                        raw.get("title")
                        or raw.get("file_name")
                        or source.get("title")
                        or source.get("file_name")
                        or header.get("title")
                        or header.get("file_name")
                    ),
                    "sender_name": _text(
                        raw.get("sender_display_name")
                        or sender.get("name")
                        or source.get("sender_display_name")
                    ),
                    "conversation_id": _text(
                        raw.get("source_conversation_id")
                        or conversation.get("id")
                        or source.get("source_conversation_id")
                    ),
                    "conversation_name": _text(
                        raw.get("source_conversation_name")
                        or conversation.get("name")
                        or source.get("source_conversation_name")
                    ),
                    "conversation_type": _text(
                        raw.get("source_conversation_type")
                        or conversation.get("type")
                        or source.get("source_conversation_type")
                    ),
                    "conversation_platform": _text(
                        raw.get("source_platform")
                        or conversation.get("platform")
                        or source.get("platform")
                    ),
                    "sent_at": _text(raw.get("sent_at") or source.get("sent_at")),
                    "best_score": score,
                    "chunks": {},
                },
            )
            item["best_score"] = max(float(item["best_score"]), score)
            item["chunks"].setdefault(
                chunk_id,
                {
                    "chunk_id": chunk_id,
                    "text": str(raw.get("content") or raw.get("text") or ""),
                    "score": score,
                    "score_type": str(raw.get("score_type") or "rrf"),
                    "position": raw.get("position") or raw.get("source_locator"),
                },
            )

    results: list[ContentResult] = []
    for item in grouped.values():
        chunks = sorted(
            item.pop("chunks").values(),
            key=lambda chunk: chunk["score"],
            reverse=True,
        )[:3]
        if not chunks:
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


def _tree_diagnostics(responses: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = [
        response.get("diagnostics")
        for response in responses
        if isinstance(response.get("diagnostics"), dict)
    ]
    first = diagnostics[0] if diagnostics else {}
    entity_ids: list[str] = []
    for item in diagnostics:
        entity_scope = item.get("entity_scope")
        values = (
            entity_scope.get("entity_ids")
            if isinstance(entity_scope, dict)
            else None
        )
        for entity_id in values or []:
            if entity_id not in entity_ids:
                entity_ids.append(str(entity_id))
    return {
        "tree_mode": str(first.get("tree_mode") or "tree"),
        "effective_execution_path": str(
            first.get("effective_execution_path") or "tree"
        ),
        "fallback_reason": first.get("fallback_reason"),
        "entity_ids": entity_ids,
        "locate_ms": max(
            (float(item.get("locate_ms") or 0) for item in diagnostics),
            default=0.0,
        ),
        "metadata_coverage": str(first.get("metadata_coverage") or "complete"),
    }


def _tree_output(
    results: list[ContentResult],
    *,
    responses: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    retrieval_path: str,
    has_more: bool | None = None,
    metadata_coverage: str | None = None,
    summary: str,
) -> SearchTreeOutput:
    return SearchTreeOutput(
        results=results,
        evidence=_answer_evidence(results),
        returned_source_count=len(results),
        returned_chunk_count=sum(len(item.chunks) for item in results),
        has_more=(
            any(bool(item.get("has_more")) for item in responses)
            if has_more is None
            else has_more
        ),
        summary=summary,
        metadata_coverage=metadata_coverage or diagnostics["metadata_coverage"],
        retrieval_path=retrieval_path,
        tree_mode=diagnostics["tree_mode"],
        fallback_reason=diagnostics["fallback_reason"],
        entity_ids=diagnostics["entity_ids"],
        locate_ms=diagnostics["locate_ms"],
    )


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
