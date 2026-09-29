from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.models import TaskEnvelope


class ChatIngress:
    def create_task(
        self,
        owner_user_id: str,
        input: Mapping[str, Any],
        *,
        source_ref: Mapping[str, Any] | None = None,
        constraints: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskEnvelope:
        return TaskEnvelope(
            task_id=task_id or str(uuid4()),
            source_type="chat",
            owner_user_id=owner_user_id,
            input=dict(input),
            source_ref=dict(source_ref or {}),
            constraints=dict(constraints or {}),
            created_at=created_at or datetime.now(timezone.utc),
        )
