from __future__ import annotations

import json
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
    QAConversationBody,
    QATitleBody,
    SearchBody,
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
