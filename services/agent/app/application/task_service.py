"""Task lifecycle services backing the Agent API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.kernel.errors import AgentContractError
from app.kernel.events import new_outbox_event, new_task_event, utcnow
from app.kernel.models import (
    ConversationRecord,
    MessageRecord,
    Observation,
    Plan,
    TaskEvent,
    TaskInput,
    TaskRecord,
)
from app.kernel.protocols import AgentStore
from app.kernel.states import (
    EVENT_TASK_ACCEPTED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_INPUT_RECEIVED,
    TERMINAL_TASK_STATUSES,
    ensure_task_transition,
)


class TaskNotFoundError(AgentContractError):
    pass


class TaskPermissionError(AgentContractError):
    pass


class ConversationNotFoundError(AgentContractError):
    pass


class TaskStateError(AgentContractError):
    pass


class TaskService:
    def __init__(self, store: AgentStore) -> None:
        self.store = store

    def create_task(
        self,
        *,
        owner_user_id: str,
        source_type: str = "chat",
        payload: dict[str, Any],
        source_ref: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        client_message_id: str | None = None,
        task_id: str | None = None,
        created_at: datetime | None = None,
        conversation_id: str | None = None,
        organization_id: str | None = None,
    ) -> TaskRecord:
        moment = created_at or datetime.now(timezone.utc)
        resolved_id = task_id or str(uuid4())
        idempotency_key = (
            f"{source_type}:{owner_user_id}:{client_message_id}" if client_message_id else None
        )
        provided_conversation: ConversationRecord | None = None
        if conversation_id:
            provided_conversation = self.store.get_conversation(conversation_id)
            if provided_conversation is None:
                raise ConversationNotFoundError(
                    f"unknown conversation: {conversation_id}"
                )
            if provided_conversation.owner_user_id != owner_user_id:
                raise TaskPermissionError(
                    "conversation does not belong to the current user"
                )

        if idempotency_key:
            existing = self.store.find_task_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing

        conversation = provided_conversation
        if conversation is None and source_type == "chat":
            conversation = ConversationRecord(
                conversation_id=str(uuid4()),
                owner_user_id=owner_user_id,
                organization_id=organization_id,
                title=self._conversation_title(payload),
                source="agent",
                created_at=moment,
                updated_at=moment,
            )

        request_message_id = str(uuid4()) if conversation is not None else None
        response_message_id = str(uuid4()) if conversation is not None else None
        record = TaskRecord(
            task_id=resolved_id,
            source_type=source_type,
            owner_user_id=owner_user_id,
            status="received",
            input=dict(payload),
            source_ref=dict(source_ref or {}),
            constraints=dict(constraints or {}),
            idempotency_key=idempotency_key,
            conversation_id=conversation.conversation_id if conversation else None,
            request_message_id=request_message_id,
            response_message_id=response_message_id,
            created_at=moment,
            updated_at=moment,
        )
        events = [
            new_task_event(
                resolved_id,
                EVENT_TASK_ACCEPTED,
                {"source_type": source_type, "owner_user_id": owner_user_id},
                occurred_at=moment,
            )
        ]
        outbox = [
            new_outbox_event(
                resolved_id,
                "agent.task.wakeup",
                {"reason": "accepted"},
                created_at=moment,
            )
        ]
        inputs = [
            TaskInput(
                input_id=str(uuid4()),
                task_id=resolved_id,
                version=1,
                payload=dict(payload),
                created_at=moment,
            )
        ]
        messages: list[MessageRecord] = []
        if conversation is not None:
            messages = [
                MessageRecord(
                    message_id=request_message_id or str(uuid4()),
                    conversation_id=conversation.conversation_id,
                    role="user",
                    content=str(payload.get("text") or ""),
                    status="completed",
                    task_id=resolved_id,
                    client_message_id=client_message_id,
                    created_at=moment,
                    updated_at=moment,
                ),
                MessageRecord(
                    message_id=response_message_id or str(uuid4()),
                    conversation_id=conversation.conversation_id,
                    role="assistant",
                    content="",
                    status="pending",
                    task_id=resolved_id,
                    created_at=moment + timedelta(microseconds=1),
                    updated_at=moment + timedelta(microseconds=1),
                ),
            ]
        task = self.store.create_task(
            record,
            events=events,
            outbox_events=outbox,
            inputs=inputs,
            conversation=conversation,
            messages=messages,
        )
        return task

    @staticmethod
    def _conversation_title(payload: dict[str, Any]) -> str:
        text = " ".join(str(payload.get("text") or "").split())
        if not text:
            return "新的对话"
        return text if len(text) <= 200 else text[:197] + "..."

    def sync_task_messages(self, task_id: str) -> None:
        """Project the terminal Task state onto its assistant message.

        Plan steps and observations remain execution detail; this is only the
        user-visible answer, citations and status defined by the Phase 1 model.
        """

        task = self.store.get_task(task_id)
        if task is None or not task.response_message_id:
            return
        message = self.store.get_message(task.response_message_id)
        if message is None:
            return

        status = message.status
        content = message.content
        citations = list(message.citations)
        partial = ""
        if isinstance(task.result, dict):
            partial = str(task.result.get("partial_answer") or "").strip()
        if task.status == "succeeded":
            status = "completed"
            result = task.result if isinstance(task.result, dict) else {}
            answer = result.get("answer")
            if isinstance(answer, str) and answer.strip():
                content = answer
            else:
                content = content or "任务已完成"
            raw_citations = result.get("citations")
            if isinstance(raw_citations, list):
                citations = [
                    item for item in raw_citations if isinstance(item, dict)
                ]
        elif task.status in {"failed", "unknown"}:
            status = "failed"
            error = task.last_error if isinstance(task.last_error, dict) else {}
            content = partial or str(
                error.get("message")
                or error.get("classification")
                or "任务执行失败"
            )
        elif task.status == "cancelled":
            status = "cancelled"
            content = partial or content or "任务已取消"
        else:
            return

        if (
            message.status == status
            and message.content == content
            and message.citations == citations
        ):
            return

        moment = utcnow()
        message.status = status
        message.content = content
        message.citations = citations
        message.updated_at = moment
        self.store.update_message(message)
        conversation = self.store.get_conversation(message.conversation_id)
        if conversation is not None:
            conversation.last_message_at = moment
            conversation.updated_at = moment
            self.store.save_conversation(conversation)

    def get_task(self, task_id: str, *, owner_user_id: str | None = None) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task: {task_id}")
        if owner_user_id is not None and task.owner_user_id != owner_user_id:
            raise TaskPermissionError("task does not belong to the current user")
        return task

    def get_active_plan(self, task_id: str) -> Plan | None:
        return self.store.get_active_plan(task_id)

    def list_tasks(
        self,
        *,
        owner_user_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        """Read-only listing used by the API so a client can discover its Tasks."""

        return self.store.list_tasks_for_owner(owner_user_id, statuses=statuses, limit=limit)

    def list_observations(self, task_id: str) -> list[Observation]:
        return self.store.list_observations(task_id)

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[TaskEvent]:
        return self.store.list_events(task_id, after_sequence=after_sequence)

    def submit_input(
        self,
        task_id: str,
        *,
        owner_user_id: str,
        payload: dict[str, Any],
        resume: bool = False,
    ) -> TaskRecord:
        task = self.get_task(task_id, owner_user_id=owner_user_id)
        if task.status != "waiting_input":
            raise TaskStateError(f"task is not waiting for input: {task.status}")

        moment = utcnow()
        versions = self.store.list_inputs(task_id)
        next_version = (versions[-1].version + 1) if versions else 1
        self.store.add_input(
            TaskInput(
                input_id=str(uuid4()),
                task_id=task_id,
                version=next_version,
                payload=dict(payload),
                created_at=moment,
            )
        )

        # A takeover/login confirmation is not new user intent. Resume the
        # existing Plan so its ready Step is re-executed after the external
        # prerequisite has been satisfied.
        if resume and task.current_plan_id and self.store.get_active_plan(task_id):
            merged = dict(task.input)
            incoming = dict(payload)
            resume_text = incoming.pop("text", None)
            if resume_text:
                merged["resume_input"] = resume_text
            for observation in reversed(self.store.list_observations(task_id)):
                takeover = (observation.output or {}).get("takeover")
                if isinstance(takeover, dict) and takeover.get("session_id"):
                    merged["resume_session_id"] = str(takeover["session_id"])
                    break
            merged.update(incoming)
            task.input = merged
            task.result = None
            ensure_task_transition(task.status, "ready")
            task.status = "ready"
            task.updated_at = moment
            self.store.commit(
                task,
                events=[
                    new_task_event(
                        task_id,
                        EVENT_TASK_INPUT_RECEIVED,
                        {
                            "version": next_version,
                            "payload": payload,
                            "resume": True,
                        },
                        occurred_at=moment,
                    )
                ],
                outbox_events=[
                    new_outbox_event(
                        task_id,
                        "agent.task.wakeup",
                        {"reason": "input_resumed"},
                        created_at=moment,
                    )
                ],
            )
            updated = self.store.get_task(task_id)
            assert updated is not None
            return updated

        # The unfinished plan belonged to the incomplete input, so it is
        # invalidated and the runtime re-plans from the accumulated input.
        self.store.invalidate_plans(task_id)
        merged = dict(task.input)
        merged.update(payload)
        task.input = merged
        task.current_plan_id = None
        task.understanding = None
        task.result = None
        ensure_task_transition(task.status, "planning")
        task.status = "planning"
        task.updated_at = moment
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task_id,
                    EVENT_TASK_INPUT_RECEIVED,
                    {"version": next_version, "payload": payload, "resume": False},
                    occurred_at=moment,
                )
            ],
            outbox_events=[
                new_outbox_event(task_id, "agent.task.wakeup", {"reason": "input_received"}, created_at=moment)
            ],
        )
        updated = self.store.get_task(task_id)
        assert updated is not None
        return updated

    def cancel(self, task_id: str, *, owner_user_id: str) -> TaskRecord:
        task = self.get_task(task_id, owner_user_id=owner_user_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise TaskStateError(f"task is already finished: {task.status}")
        moment = utcnow()
        ensure_task_transition(task.status, "cancelled")
        task.status = "cancelled"
        task.updated_at = moment
        self.store.commit(
            task,
            events=[new_task_event(task_id, EVENT_TASK_CANCELLED, {}, occurred_at=moment)],
        )
        updated = self.store.get_task(task_id)
        assert updated is not None
        self.sync_task_messages(task_id)
        return updated
