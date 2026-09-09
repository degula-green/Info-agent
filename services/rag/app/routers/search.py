from fastapi import APIRouter, Header, HTTPException, Query

from app.infrastructure.elasticsearch import ElasticsearchUnavailable
from app.services.search_service import search as run_search

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(q: str = Query(default=""), x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    if not (x_user_id or "").strip():
        raise HTTPException(status_code=401, detail="user identity is required")
    try:
        result = run_search(q, user_id=x_user_id or "")
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    result["message"] = "RAG search endpoint is ready"
    return result
