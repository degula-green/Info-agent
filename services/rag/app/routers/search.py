from fastapi import APIRouter, Query

from app.services.search_service import search as run_search

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(q: str = Query(default="")) -> dict[str, object]:
    result = run_search(q)
    result["message"] = "RAG search endpoint is ready"
    return result
