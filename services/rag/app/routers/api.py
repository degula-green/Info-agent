from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.domain.models import SearchRequest
from app.config import settings
from app.infrastructure.elasticsearch import ElasticsearchUnavailable
from app.schemas.search import AIDocumentBody, SearchBody
from app.services.search_service import get_service


router = APIRouter(prefix="/api/v1", tags=["rag"])


def _request(body: SearchBody, *, entry: str, header_user_id: str | None) -> SearchRequest:
    if settings.environment not in {"development", "test"} and not (header_user_id or "").strip():
        raise HTTPException(status_code=401, detail="X-User-ID header is required")
    user_id = (header_user_id or body.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="user identity is required")
    return SearchRequest(
        query=body.query,
        user_id=user_id,
        organization_id=body.organization_id,
        knowledge_base_id=body.knowledge_base_id,
        entry=entry,
        sender_name=body.sender_name,
        occurred_after=body.occurred_after,
        occurred_before=body.occurred_before,
        top_k=body.top_k,
        include_protected=body.include_protected,
        conversation_id=getattr(body, "conversation_id", None),
    )


@router.post("/search/global")
def global_search(body: SearchBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    return _run_search(_request(body, entry="global", header_user_id=x_user_id))


@router.post("/search/knowledge")
def knowledge_search(body: SearchBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    return _run_search(_request(body, entry="knowledge", header_user_id=x_user_id))


@router.post("/ai/documents")
def ai_documents(body: AIDocumentBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    request = _request(body, entry="ai", header_user_id=x_user_id)
    try:
        return get_service().answer(request)
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="RAG request could not be completed") from exc


def _run_search(request: SearchRequest) -> dict[str, object]:
    try:
        response = get_service().search(request)
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="RAG request could not be completed") from exc
    return {
        "request_id": response.request_id,
        "query": request.query,
        "items": [item.as_dict() for item in response.results],
        "diagnostics": {
            "protected_scope_available": response.diagnostics.protected_scope_available,
            "protected_scope_count": response.diagnostics.protected_scope_count,
            "candidate_count": response.diagnostics.candidate_count,
            "authorized_count": response.diagnostics.authorized_count,
            "degraded": list(response.diagnostics.degraded),
            "use_vector": response.diagnostics.plan.use_vector,
        },
    }
