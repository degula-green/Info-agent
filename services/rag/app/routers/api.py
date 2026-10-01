from __future__ import annotations

import json
import hmac
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.application.rag_service import (
    AuthorizationDenied,
    AuthorizationUnavailable,
    RAGRetrievalService,
    RetrievalResponse,
    SearchUnavailable,
)
from app.application.qa_service import (
    ConversationNotFound,
    ConversationScopeMismatch,
    QAService,
)
from app.config import settings
from app.dependencies import get_qa_service, get_retrieval_service
from app.domain.rag import SearchRequest
from app.infrastructure.qa import QAUnavailable
from app.schemas.search import (
    AIDocumentBody,
    ContentSearchBody,
    QAConversationBody,
    QATitleBody,
    SearchBody,
    SourceSearchBody,
    TreeSearchBody,
)


router = APIRouter(prefix="/api/v1", tags=["rag"])


def _identity(
    body: SearchBody,
    *,
    header_user_id: str | None,
    header_organization_id: str | None,
) -> tuple[str, str, str]:
    user_id = (header_user_id or body.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="user identity is required")
    scope_type = body.scope_type
    if scope_type == "organization":
        organization_id = (header_organization_id or body.organization_id or "").strip()
        if not organization_id:
            if settings.environment in {"development", "test"}:
                organization_id = user_id
            else:
                raise HTTPException(status_code=422, detail="invalid_scope")
        return user_id, scope_type, organization_id
    return user_id, "user", user_id


def _request(
    body: SearchBody,
    *,
    entry: str,
    header_user_id: str | None,
    header_organization_id: str | None,
) -> SearchRequest:
    user_id, scope_type, scope_id = _identity(
        body,
        header_user_id=header_user_id,
        header_organization_id=header_organization_id,
    )
    return SearchRequest(
        query=body.query,
        user_id=user_id,
        scope_type=scope_type,
        scope_id=scope_id,
        knowledge_base_ids=body.clean_library_ids(),
        entry=entry,
        top_k=body.top_k,
        include_protected=body.include_protected,
        occurred_after=body.occurred_after,
        occurred_before=body.occurred_before,
        qa_mode=getattr(body, "mode", "quick"),
        conversation_id=getattr(body, "conversation_id", None),
        source_conversation_id=body.source_conversation_id,
        sender_ids=tuple(body.sender_ids),
        sender_names=tuple(body.sender_names),
        conversation_ids=tuple(body.conversation_ids),
        conversation_names=tuple(body.conversation_names),
        resource_ids=tuple(body.resource_ids),
        resource_types=tuple(body.resource_types),
        file_extensions=tuple(body.file_extensions),
        message_types=tuple(body.message_types),
        group_by_source=body.group_by_source,
    )


@router.post("/search/global")
def global_search(
    body: SearchBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: RAGRetrievalService = Depends(get_retrieval_service),
) -> dict[str, object]:
    request = _request(
        body,
        entry="global",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    return _search_response(_run_search(request, service))


@router.post("/search/knowledge")
def knowledge_search(
    body: SearchBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: RAGRetrievalService = Depends(get_retrieval_service),
) -> dict[str, object]:
    request = _request(
        body,
        entry="knowledge",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    return _search_response(_run_search(request, service))


@router.post("/search/tree")
def tree_search(
    body: TreeSearchBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: RAGRetrievalService = Depends(get_retrieval_service),
) -> dict[str, object]:
    request = _request(
        body,
        entry="tree",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    return _search_response(_run_search(request, service))


@router.post("/search/sources")
def search_sources(
    body: SourceSearchBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    x_agent_service_token: str | None = Header(default=None),
    service: RAGRetrievalService = Depends(get_retrieval_service),
) -> dict[str, object]:
    _require_agent_service(x_agent_service_token)
    request = _request(
        body,
        entry="sources",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    response = _run_search(request, service)
    return _sources_response(response, top_k=request.top_k)


@router.post("/search/content")
def search_content(
    body: ContentSearchBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    x_agent_service_token: str | None = Header(default=None),
    service: RAGRetrievalService = Depends(get_retrieval_service),
) -> dict[str, object]:
    _require_agent_service(x_agent_service_token)
    request = _request(
        body,
        entry="content",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    response = _run_search(request, service)
    return _content_response(response, top_k=request.top_k)


@router.post("/ai/documents")
def ai_documents(
    body: AIDocumentBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> dict[str, object]:
    request = _request(
        body,
        entry="ai",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )
    try:
        return service.answer(request)
    except SearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search_unavailable") from exc
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    except (QAUnavailable, ConversationNotFound, ConversationScopeMismatch) as exc:
        if isinstance(exc, ConversationNotFound):
            raise HTTPException(status_code=404, detail="conversation_not_found") from exc
        if isinstance(exc, ConversationScopeMismatch):
            raise HTTPException(status_code=403, detail="forbidden") from exc
        raise HTTPException(status_code=503, detail="qa_unavailable") from exc
    except AuthorizationUnavailable as exc:
        raise HTTPException(status_code=503, detail="authz_unavailable") from exc


@router.post("/ai/documents/stream")
def ai_documents_stream(
    body: AIDocumentBody,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> StreamingResponse:
    request = _request(
        body,
        entry="ai",
        header_user_id=x_user_id,
        header_organization_id=x_organization_id,
    )

    def events() -> Iterator[str]:
        try:
            for event, payload in service.answer_stream(request):
                yield _sse(event, payload)
        except Exception as exc:
            yield _sse("error", {"code": "qa_unavailable", "message": str(exc)})
            yield _sse("done", {"answer": None, "diagnostics": {}})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/qa/conversations")
def list_qa_conversations(
    x_user_id: str | None = Header(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: QAService = Depends(get_qa_service),
) -> dict[str, Any]:
    user_id = _header_user(x_user_id)
    try:
        items, total = service.list_conversations(
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="qa_history_unavailable") from exc
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.post("/qa/conversations")
def create_qa_conversation(
    body: QAConversationBody | None = None,
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> dict[str, Any]:
    user_id = _header_user(x_user_id)
    value = body or QAConversationBody()
    scope_type = value.scope_type
    scope_id = (
        (x_organization_id or value.organization_id or "").strip()
        if scope_type == "organization"
        else user_id
    )
    if not scope_id and settings.environment in {"development", "test"}:
        scope_id = user_id
    if not scope_id:
        raise HTTPException(status_code=422, detail="invalid_scope")
    try:
        conversation_id = service.create_conversation(
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            title=value.title,
            retrieval_mode=value.retrieval_mode,
            knowledge_base_ids=value.knowledge_base_ids,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="qa_history_unavailable") from exc
    return {
        "id": conversation_id,
        "title": value.title,
        "retrieval_mode": value.retrieval_mode,
        "knowledge_base_ids": value.knowledge_base_ids,
        "message_count": 0,
    }


@router.get("/qa/conversations/{conversation_id}")
def get_qa_conversation(
    conversation_id: str,
    x_user_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> dict[str, Any]:
    try:
        value = service.get_conversation(
            user_id=_header_user(x_user_id),
            conversation_id=conversation_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="qa_history_unavailable") from exc
    if not value:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return value


@router.patch("/qa/conversations/{conversation_id}")
def rename_qa_conversation(
    conversation_id: str,
    body: QATitleBody,
    x_user_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> dict[str, Any]:
    try:
        renamed = service.rename_conversation(
            user_id=_header_user(x_user_id),
            conversation_id=conversation_id,
            title=body.title,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="qa_history_unavailable") from exc
    if not renamed:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return {"id": conversation_id, "title": body.title}


@router.delete("/qa/conversations/{conversation_id}")
def delete_qa_conversation(
    conversation_id: str,
    x_user_id: str | None = Header(default=None),
    service: QAService = Depends(get_qa_service),
) -> dict[str, str]:
    try:
        deleted = service.delete_conversation(
            user_id=_header_user(x_user_id),
            conversation_id=conversation_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="qa_history_unavailable") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return {"status": "deleted"}


def _run_search(
    request: SearchRequest,
    service: RAGRetrievalService,
) -> RetrievalResponse:
    try:
        return service.search(request)
    except SearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search_unavailable") from exc
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    except AuthorizationUnavailable as exc:
        raise HTTPException(status_code=503, detail="authz_unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="search_unavailable") from exc


def _search_response(response: RetrievalResponse) -> dict[str, Any]:
    return {
        "request_id": response.request_id,
        "items": [_legacy_item(item) for item in response.results],
        "citations": [],
        "diagnostics": response.diagnostics,
    }


def _require_agent_service(token: str | None) -> None:
    expected = str(settings.agent_service_token or "").strip()
    supplied = str(token or "").strip()
    if expected and not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not expected and settings.environment not in {"development", "test"}:
        raise HTTPException(status_code=503, detail="agent_service_token_unconfigured")


def _sources_response(response: RetrievalResponse, *, top_k: int) -> dict[str, Any]:
    items = [_source_item(result) for result in response.results]
    resource_ids = [item["resource_id"] for item in items if item.get("resource_id")]
    return {
        "request_id": response.request_id,
        "items": items,
        "resource_ids": resource_ids,
        "returned_count": len(items),
        "has_more": len(items) >= max(1, top_k),
        "diagnostics": {
            **response.diagnostics,
            "metadata_coverage": _metadata_coverage(response.results),
        },
    }


def _source_item(result: Any) -> dict[str, Any]:
    source = result.source
    return {
        "resource_id": source.get("resource_id"),
        "resource_type": source.get("resource_type"),
        "knowledge_item_id": source.get("knowledge_item_id"),
        "title": source.get("title") or source.get("file_name"),
        "file_name": source.get("file_name"),
        "message_type": source.get("message_type"),
        "sender": {
            "id": source.get("sender_identity_id"),
            "name": source.get("sender_display_name"),
            "platform": source.get("sender_platform"),
        },
        "conversation": {
            "id": source.get("source_conversation_id"),
            "name": source.get("source_conversation_name"),
            "type": source.get("source_conversation_type"),
            "platform": source.get("source_platform"),
        },
        "knowledge_base": {"id": source.get("knowledge_base_id")},
        "sent_at": source.get("sent_at"),
        "score": result.score,
        "score_type": "rrf",
        "preview": _preview(result.content),
    }


def _content_response(response: RetrievalResponse, *, top_k: int) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for result in response.results:
        source = result.source
        key = ":".join(
            (
                str(source.get("resource_type") or ""),
                str(source.get("resource_id") or result.chunk_id),
                str(source.get("content_version") or ""),
            )
        )
        item = grouped.setdefault(
            key,
            {
                "resource_id": source.get("resource_id"),
                "resource_type": source.get("resource_type"),
                "knowledge_item_id": source.get("knowledge_item_id"),
                "title": source.get("title") or source.get("file_name"),
                "sender": {
                    "id": source.get("sender_identity_id"),
                    "name": source.get("sender_display_name"),
                    "platform": source.get("sender_platform"),
                },
                "conversation": {
                    "id": source.get("source_conversation_id"),
                    "name": source.get("source_conversation_name"),
                    "type": source.get("source_conversation_type"),
                    "platform": source.get("source_platform"),
                },
                "sent_at": source.get("sent_at"),
                "best_score": result.score,
                "score_type": "rrf",
                "matched_chunk_count": 0,
                "chunks": [],
            },
        )
        item["best_score"] = max(float(item["best_score"]), float(result.score))
        if len(item["chunks"]) >= 3:
            continue
        item["matched_chunk_count"] += 1
        item["chunks"].append(
            {
                "chunk_id": result.chunk_id,
                "text": result.content,
                "score": result.score,
                "score_type": "rrf",
                "position": dict(source.get("source_locator") or {}) or None,
            }
        )
    items = sorted(grouped.values(), key=lambda value: value["best_score"], reverse=True)
    items = items[: max(1, top_k)]
    return {
        "request_id": response.request_id,
        "items": items,
        "returned_source_count": len(items),
        "returned_chunk_count": sum(len(item["chunks"]) for item in items),
        "has_more": len(items) >= max(1, top_k),
        "diagnostics": {
            **response.diagnostics,
            "metadata_coverage": _metadata_coverage(response.results),
        },
    }


def _metadata_coverage(results: list[Any]) -> str:
    conversation_scoped = [
        result
        for result in results
        if result.source.get("source_conversation_id")
    ]
    if not conversation_scoped:
        return "complete"
    missing = any(
        not all(
            (
                result.source.get("sender_identity_id"),
                result.source.get("sender_display_name"),
                result.source.get("source_conversation_name"),
                result.source.get("source_platform"),
            )
        )
        for result in conversation_scoped
    )
    return "partial" if missing else "complete"


def _preview(value: str, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _legacy_item(result: Any) -> dict[str, Any]:
    """Flatten the MVP source projection for the existing Web client."""
    item = result.safe_dict()
    source = item.pop("source", {}) or {}
    return {
        **source,
        **item,
        "source": source.get("platform") or source.get("resource_type") or "knowledge",
    }


def _header_user(value: str | None) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user_id


def _sse(event: str, value: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
