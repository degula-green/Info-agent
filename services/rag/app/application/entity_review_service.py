from __future__ import annotations

from typing import Any

from app.application.mvp_ports import EntityRegistryRepository


class InvalidReviewIdempotency(RuntimeError):
    pass


class EntityReviewService:
    def __init__(self, *, repository: EntityRegistryRepository) -> None:
        self.repository = repository

    def review(
        self,
        *,
        scope_type: str,
        scope_id: str,
        candidate_id: str,
        reviewer_id: str,
        review_request_id: str | None,
        idempotency_key: str | None,
        action: str,
        expected_status: str | None,
        canonical_name: str | None,
        domain: str | None,
        target_entity_id: str | None,
        note: str | None,
    ) -> dict[str, Any]:
        request_id = _resolve_request_id(review_request_id, idempotency_key)
        return self.repository.review_candidate(
            scope_type=scope_type,
            scope_id=scope_id,
            candidate_id=candidate_id,
            reviewer_id=reviewer_id,
            review_request_id=request_id,
            action=action,
            expected_status=expected_status,
            canonical_name=canonical_name,
            domain=domain,
            target_entity_id=target_entity_id,
            note=note,
        )


def _resolve_request_id(
    review_request_id: str | None,
    idempotency_key: str | None,
) -> str:
    review_value = str(review_request_id or "").strip()
    idempotency_value = str(idempotency_key or "").strip()
    if not review_value and not idempotency_value:
        raise InvalidReviewIdempotency(
            "review_request_id or Idempotency-Key is required"
        )
    if review_value and idempotency_value and review_value != idempotency_value:
        raise InvalidReviewIdempotency(
            "review_request_id and Idempotency-Key must match"
        )
    return review_value or idempotency_value
