from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from app.application.bootstrap import build_retrieval_service
from app.application.rag_service import SearchUnavailable
from app.domain.rag import SearchRequest


router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(
    q: str = Query(default=""),
    top_k: int = Query(default=8, ge=1, le=50),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, object]:
    user_id = str(x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    if x_organization_id:
        scope_type, scope_id = "organization", str(x_organization_id)
    else:
        scope_type, scope_id = "user", user_id
    try:
        response = build_retrieval_service().search(SearchRequest(
            query=q,
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            entry="global",
            top_k=top_k,
        ))
    except SearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search_unavailable") from exc
    return {
        "request_id": response.request_id,
        "query": q,
        "items": [item.safe_dict() for item in response.results],
        "diagnostics": response.diagnostics,
        "message": "RAG search endpoint is ready",
    }
