import hmac

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
    authorization: str | None = Header(default=None),
    container: ApplicationContainer = Depends(get_container),
) -> Response:
    """Prometheus scrape endpoint.

    Gated by the internal token rather than left open: the series carry scope
    ids, which are tenant identifiers. `RAG_METRICS_ENABLED=false` turns it off
    for deployments that scrape something else.

    Two header shapes are accepted because Prometheus scrape configs have no
    free-form header field: they can only send `Authorization: Bearer`, so a
    deployment that cannot add an `X-RAG-Internal-Token` sidecar would otherwise
    have no way to authenticate at all.
    """
    expected = settings.rag_internal_token
    if not expected:
        raise HTTPException(status_code=500, detail="RAG_INTERNAL_TOKEN is not configured")
    presented = _presented_token(x_rag_internal_token, authorization)
    # compare_digest: this is a shared secret, so the comparison should not
    # leak its length or prefix through timing.
    if presented is None or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="internal token required")
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    return Response(
        content=container.tree_metrics_service.prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _presented_token(
    x_rag_internal_token: str | None, authorization: str | None
) -> str | None:
    """The token from either accepted header, or None when neither carries one."""
    if x_rag_internal_token:
        return x_rag_internal_token
    header = str(authorization or "").strip()
    if header.lower().startswith("bearer "):
        value = header[len("bearer ") :].strip()
        return value or None
    return None
