"""Download endpoint for generated weekly reports.

The artifact lives in the Agent's own MinIO bucket for 24 hours. This endpoint
streams it back to its owner: the preview iframe and the download button both
use it, so the browser never needs direct object-store credentials.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.auth import AuthenticatedUser, current_user
from app.container import AgentContainer
from app.infrastructure.agent_attachments import (
    AgentAttachmentError,
    AgentAttachmentNotFound,
    AgentAttachmentStore,
)

router = APIRouter(prefix="/api/agent/v1")

_container: AgentContainer | None = None


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_attachment_store() -> AgentAttachmentStore:
    if _container is None or _container.attachment_store is None:
        raise HTTPException(
            status_code=503, detail="agent attachment storage is not configured"
        )
    return _container.attachment_store


@router.get("/reports/{attachment_id}")
def download_report(
    attachment_id: str,
    download: bool = Query(default=False),
    user: AuthenticatedUser = Depends(current_user),
    store: AgentAttachmentStore = Depends(get_attachment_store),
) -> Response:
    metadata = store.metadata(attachment_id)
    # A missing record and someone else's record are the same answer: the
    # caller must not learn that another user's report exists.
    if not metadata or str(metadata.get("owner_id")) != str(user.user_id):
        raise HTTPException(status_code=404, detail="report not found")
    try:
        payload = store.read(attachment_id)
    except AgentAttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    except AgentAttachmentError as exc:
        raise HTTPException(status_code=503, detail="report is unavailable") from exc
    file_name = str(metadata.get("file_name") or "weekly-report.docx")
    disposition = "attachment" if download else "inline"
    return Response(
        content=payload,
        media_type=str(metadata.get("mime_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{quote(file_name)}"
            ),
            "Content-Length": str(len(payload)),
            "Cache-Control": "private, no-store",
        },
    )
