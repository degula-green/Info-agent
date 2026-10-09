from fastapi import APIRouter, Depends, Header, HTTPException, Response

from app.application.bootstrap import ApplicationContainer
from app.config import settings
from app.dependencies import get_container

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"service": "rag", "status": "ok"}


@router.get("/metrics")
def metrics(
    x_rag_internal_token: str | None = Header(default=None),
    container: ApplicationContainer = Depends(get_container),
) -> Response:
    """Prometheus scrape endpoint.

    Gated by the internal token rather than left open: the series carry scope
    ids, which are tenant identifiers. `RAG_METRICS_ENABLED=false` turns it off
    for deployments that scrape something else.
    """
    expected = settings.rag_internal_token
    if not expected:
        raise HTTPException(status_code=500, detail="RAG_INTERNAL_TOKEN is not configured")
    if x_rag_internal_token != expected:
        raise HTTPException(status_code=403, detail="internal token required")
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    return Response(
        content=container.tree_metrics_service.prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
