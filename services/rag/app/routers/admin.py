from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.application.bootstrap import build_repository, build_retrieval_service
from app.domain.rag import AccessCheck


router = APIRouter(prefix="/api/v1/admin", tags=["rag-admin"])


class CandidateReviewBody(BaseModel):
    review_request_id: str | None = None
    action: str = Field(pattern="^(promote|merge|ignore|defer)$")
    target_entity_id: str | None = None
    canonical_name: str | None = None
    domain: str | None = None
    note: str | None = None
    expected_status: str | None = None


def _admin_scope(
    *,
    x_user_id: str | None,
    x_organization_id: str | None,
    scope_type: str,
) -> tuple[str, str, str]:
    user_id = str(x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    if scope_type == "organization":
        scope_id = str(x_organization_id or "").strip()
        if not scope_id:
            raise HTTPException(status_code=422, detail="invalid_scope")
    else:
        scope_id = user_id
    service = build_retrieval_service()
    decisions = service.authorization.check_batch(
        user_id=user_id,
        scope_type=scope_type,
        scope_id=scope_id,
        checks=[AccessCheck("organization", "admin", scope_id, "manage")],
    )
    if decisions != [True]:
        raise HTTPException(status_code=403, detail="forbidden")
    return user_id, scope_type, scope_id


@router.get("/entity-tree")
def get_entity_tree(
    scope_type: str = Query(default="organization", pattern="^(organization|user)$"),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _, scope_type, scope_id = _admin_scope(
        x_user_id=x_user_id,
        x_organization_id=x_organization_id,
        scope_type=scope_type,
    )
    return build_repository().get_tree(scope_type=scope_type, scope_id=scope_id)


@router.get("/entity-tree/nodes/{node_id}")
def get_entity_tree_node(
    node_id: str,
    scope_type: str = Query(default="organization", pattern="^(organization|user)$"),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _, scope_type, scope_id = _admin_scope(
        x_user_id=x_user_id,
        x_organization_id=x_organization_id,
        scope_type=scope_type,
    )
    tree = build_repository().get_tree(scope_type=scope_type, scope_id=scope_id)
    node = next((item for item in tree["nodes"] if item["node_id"] == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail="node_not_found")
    node["children"] = [
        item for item in tree["nodes"] if item.get("parent_id") == node_id
    ]
    return node


@router.get("/entity-candidates")
def list_candidates(
    status: str | None = None,
    domain: str | None = None,
    query: str | None = None,
    min_score: float = Query(default=0, ge=0, le=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    scope_type: str = Query(default="organization", pattern="^(organization|user)$"),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _, scope_type, scope_id = _admin_scope(
        x_user_id=x_user_id,
        x_organization_id=x_organization_id,
        scope_type=scope_type,
    )
    items, total = build_repository().list_candidates(
        scope_type=scope_type,
        scope_id=scope_id,
        status=status,
        domain=domain,
        query=query,
        page=page,
        page_size=page_size,
    )
    items = [item for item in items if float(item.get("score") or 0) >= min_score]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.get("/entity-candidates/{candidate_id}")
def get_candidate(
    candidate_id: str,
    scope_type: str = Query(default="organization", pattern="^(organization|user)$"),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _, scope_type, scope_id = _admin_scope(
        x_user_id=x_user_id,
        x_organization_id=x_organization_id,
        scope_type=scope_type,
    )
    value = build_repository().get_candidate(
        scope_type=scope_type,
        scope_id=scope_id,
        candidate_id=candidate_id,
    )
    if not value:
        raise HTTPException(status_code=404, detail="candidate_not_found")
    return value


@router.post("/entity-candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: str,
    body: CandidateReviewBody,
    scope_type: str = Query(default="organization", pattern="^(organization|user)$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_user_id: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
) -> dict[str, Any]:
    user_id, scope_type, scope_id = _admin_scope(
        x_user_id=x_user_id,
        x_organization_id=x_organization_id,
        scope_type=scope_type,
    )
    request_id = body.review_request_id or idempotency_key or str(uuid.uuid4())
    try:
        return build_repository().review_candidate(
            scope_type=scope_type,
            scope_id=scope_id,
            candidate_id=candidate_id,
            reviewer_id=user_id,
            review_request_id=request_id,
            action=body.action,
            expected_status=body.expected_status,
            canonical_name=body.canonical_name,
            domain=body.domain,
            target_entity_id=body.target_entity_id,
            note=body.note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
