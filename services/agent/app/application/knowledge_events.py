"""knowledge.ready → conversation snapshot → pre-filter → Task fan-out.

Knowledge decides visibility (who may see the conversation). This service only
decides whether the event is actionable and, if so, creates one Task per
eligible owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.infrastructure.knowledge.client import (
    KnowledgeClient,
    KnowledgeError,
    KnowledgeForbidden,
    KnowledgeItemNotFound,
    KnowledgeItemNotReady,
    KnowledgeRejected,
    KnowledgeUnavailable,
)
from app.ingress.knowledge_events import KnowledgeEventIngress

ACK = "ack"
RETRY = "retry"

logger = logging.getLogger("agent.knowledge_events")


@dataclass(frozen=True)
class EventOutcome:
    action: str
    reason: str
    task_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ack(self) -> bool:
        return self.action == ACK


class KnowledgeEventService:
    def __init__(
        self,
        *,
        ingress: KnowledgeEventIngress,
        knowledge: KnowledgeClient,
        task_service,
        log: logging.Logger | None = None,
    ) -> None:
        self.ingress = ingress
        self.knowledge = knowledge
        self.task_service = task_service
        self.log = log or logger

    def handle(self, event: Mapping[str, Any]) -> EventOutcome:
        if str(event.get("event_type") or "") != "knowledge.ready":
            return EventOutcome(ACK, "unexpected_event_type")

        payload = event.get("payload")
        fields = payload if isinstance(payload, Mapping) else event
        knowledge_item_id = str(fields.get("knowledge_item_id") or "").strip()
        if not knowledge_item_id:
            return EventOutcome(ACK, "missing_knowledge_item_id")
        if fields.get("attachment_id") or fields.get("source_attachment_id"):
            return EventOutcome(ACK, "attachment_item")

        try:
            snapshot = self.knowledge.conversation_snapshot(knowledge_item_id)
        except KnowledgeItemNotReady as exc:
            return EventOutcome(RETRY, exc.code or "knowledge_item_not_ready")
        except KnowledgeUnavailable as exc:
            return EventOutcome(RETRY, "knowledge_unavailable", detail={"error": str(exc)})
        except (KnowledgeItemNotFound, KnowledgeForbidden, KnowledgeRejected) as exc:
            self.log.warning(
                "knowledge snapshot dropped for item %s: %s", knowledge_item_id, exc.code or exc
            )
            return EventOutcome(ACK, exc.code or "snapshot_rejected")
        except KnowledgeError as exc:  # pragma: no cover - defensive
            return EventOutcome(RETRY, "knowledge_error", detail={"error": str(exc)})

        return self._fan_out(event, knowledge_item_id, snapshot)

    # -- internals ---------------------------------------------------------

    def _fan_out(
        self, event: Mapping[str, Any], knowledge_item_id: str, snapshot: Mapping[str, Any]
    ) -> EventOutcome:
        if snapshot.get("source_attachment_id"):
            return EventOutcome(ACK, "attachment_item")

        visibility = str(snapshot.get("visibility") or "resolved").strip().lower()
        excluded = tuple(
            str(entry.get("reason_code") or "")
            for entry in (snapshot.get("excluded_members") or [])
            if isinstance(entry, Mapping) and entry.get("reason_code")
        )
        if visibility != "resolved":
            self.log.warning(
                "conversation visibility unresolved for item %s: %s",
                knowledge_item_id,
                visibility,
            )
            return EventOutcome(ACK, "visibility_unknown", reason_codes=excluded)

        if not self.ingress.is_candidate(event, snapshot, text=snapshot.get("text")):
            return EventOutcome(ACK, "prefiltered", reason_codes=excluded)

        owners = self.ingress.eligible_owners(snapshot)
        if not owners:
            self.log.info(
                "no eligible owner for item %s (excluded: %s)", knowledge_item_id, excluded
            )
            return EventOutcome(ACK, "no_eligible_owner", reason_codes=excluded)

        envelopes = self.ingress.create_tasks(event, snapshot, text=snapshot.get("text"))
        if not envelopes:
            return EventOutcome(ACK, "prefiltered", reason_codes=excluded)

        client_message_id = self.ingress.client_message_id(
            knowledge_item_id, snapshot.get("content_version")
        )
        task_ids: list[str] = []
        for envelope in envelopes:
            record = self.task_service.create_task(
                owner_user_id=envelope.owner_user_id,
                source_type="knowledge_event",
                payload=dict(envelope.input),
                source_ref=dict(envelope.source_ref),
                constraints=dict(envelope.constraints),
                client_message_id=client_message_id,
            )
            task_ids.append(record.task_id)

        self.log.info(
            "fanned out item %s to %s owner(s); excluded=%s",
            knowledge_item_id,
            len(task_ids),
            excluded,
        )
        return EventOutcome(
            ACK,
            "fanout",
            task_ids=tuple(task_ids),
            reason_codes=excluded,
            detail={"eligible_owners": len(owners)},
        )
