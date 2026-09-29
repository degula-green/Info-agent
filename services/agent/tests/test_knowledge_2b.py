"""Stage 2b: the same Agent loop against the real Knowledge service.

Skipped unless the Knowledge service is reachable. The test seeds one ready
group message with :mod:`services/knowledge/cmd/agentseed`, then drives the real
HTTP contract end to end. Point it at a running service with::

    AGENT_TEST_KNOWLEDGE_URL=http://127.0.0.1:8099
    AGENT_TEST_KNOWLEDGE_TOKEN=<KNOWLEDGE_INTERNAL_SERVICE_TOKEN>

Knowledge's only remaining Agent interface is the conversation snapshot: the
to-do itself is written into the Agent's own ledger, so nothing here reaches a
calendar provider.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.infrastructure.knowledge.client import HttpKnowledgeClient
from tests.support import build_step2_container, knowledge_event

KNOWLEDGE_URL = os.getenv("AGENT_TEST_KNOWLEDGE_URL", "")
KNOWLEDGE_TOKEN = os.getenv("AGENT_TEST_KNOWLEDGE_TOKEN", "")
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

pytestmark = pytest.mark.skipif(
    not (KNOWLEDGE_URL and KNOWLEDGE_TOKEN),
    reason="AGENT_TEST_KNOWLEDGE_URL and AGENT_TEST_KNOWLEDGE_TOKEN are required",
)


@pytest.fixture(scope="module")
def seeded() -> dict:
    result = subprocess.run(
        ["go", "run", "./cmd/agentseed"],
        cwd=KNOWLEDGE_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        pytest.skip(f"agentseed failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def build_client() -> HttpKnowledgeClient:
    return HttpKnowledgeClient(base_url=KNOWLEDGE_URL, token=KNOWLEDGE_TOKEN, timeout_seconds=10)


def build_container(client, **settings):
    container, store, _publisher, _client = build_step2_container(knowledge=client, **settings)
    return container, store


def test_snapshot_exposes_only_the_mapped_member(seeded) -> None:
    client = build_client()

    snapshot = client.conversation_snapshot(seeded["knowledge_item_id"])

    assert snapshot["visibility"] == "resolved"
    owners = [item["owner_user_id"] for item in snapshot["eligible_owners"]]
    assert owners == [seeded["owner_user_id"]]
    assert snapshot["conversation_type"] == "group"
    assert snapshot["platform"] == "feishu"
    assert snapshot["text"] == seeded["text"]
    reason_codes = {item["reason_code"] for item in snapshot["excluded_members"]}
    # Repeated seed runs leave earlier members behind, so only require the
    # unmapped reason to be present.
    assert "identity_unmapped" in reason_codes


def test_collected_message_completes_the_whole_loop_against_knowledge(seeded) -> None:
    client = build_client()
    container, store = build_container(client)

    outcome = container.knowledge_events.handle(
        knowledge_event(knowledge_item_id=seeded["knowledge_item_id"])
    )
    assert len(outcome.task_ids) == 1
    task_id = outcome.task_ids[0]
    assert store.get_task(task_id).owner_user_id == seeded["owner_user_id"]

    assert container.execution_service.run_task(task_id) == "waiting_approval"
    approval = [item for item in store.list_approvals(task_id=task_id) if item.status == "waiting_approval"][-1]
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id=seeded["owner_user_id"], approve=True
    )
    assert container.execution_service.run_task(task_id) == "succeeded"

    observation = store.list_observations(task_id)[0]
    assert observation.status == "succeeded"
    # The write lands in the Agent's own ledger: no Knowledge call is involved.
    assert observation.output["todo_id"]
    assert container.todo_store.list_todos(owner_user_id=seeded["owner_user_id"])


def test_chat_entry_also_creates_the_todo_locally(seeded) -> None:
    client = build_client()
    container, store = build_container(client)
    task = container.task_service.create_task(
        owner_user_id=seeded["owner_user_id"], payload={"text": "明天晚上八点开个评审会"}
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    approval = [item for item in store.list_approvals(task_id=task.task_id) if item.status == "waiting_approval"][-1]
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id=seeded["owner_user_id"], approve=True
    )
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    observation = store.list_observations(task.task_id)[0]
    assert observation.output["todo_id"]
    stored = container.todo_store.list_todos(owner_user_id=seeded["owner_user_id"])[-1]
    assert stored.title
