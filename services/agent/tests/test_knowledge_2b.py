"""Stage 2b: the same Agent loop against the real Knowledge service.

Skipped unless the Knowledge service is reachable. The test seeds one ready
group message with :mod:`services/knowledge/cmd/agentseed`, then drives the real
HTTP contract end to end. Point it at a running service with::

    AGENT_TEST_KNOWLEDGE_URL=http://127.0.0.1:8099
    AGENT_TEST_KNOWLEDGE_TOKEN=<KNOWLEDGE_INTERNAL_SERVICE_TOKEN>
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.infrastructure.knowledge.client import CalendarNotBound, HttpKnowledgeClient
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
    assert observation.output["event_id"].startswith("fake-event-")
    assert observation.output["request_id"] == (
        f"knowledge_event:{seeded['owner_user_id']}:{seeded['knowledge_item_id']}:1"
    )


def test_knowledge_deduplicates_the_same_request_id(seeded) -> None:
    client = build_client()
    payload = {
        "request_id": f"b2b-dedupe:{seeded['knowledge_item_id']}",
        "owner_user_id": seeded["owner_user_id"],
        "provider": "feishu",
        "title": "重复请求去重验证",
        "start_time": "2026-09-27T01:00:00Z",
        "end_time": "2026-09-27T02:00:00Z",
        "timezone": "Asia/Shanghai",
    }

    first = client.create_calendar_event(payload)
    second = client.create_calendar_event(payload)

    assert first["status"] == "created"
    assert second["status"] == "already_exists"
    assert first["event_id"] == second["event_id"]


def test_chat_entry_also_writes_through_the_real_knowledge(seeded) -> None:
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
    assert observation.output["event_id"].startswith("fake-event-")
    assert observation.output["request_id"] == f"chat:{seeded['owner_user_id']}:{task.task_id}"


def test_unbound_owner_is_reported_as_not_bound(seeded) -> None:
    client = build_client()
    payload = {
        "request_id": f"b2b-unbound:{seeded['knowledge_item_id']}",
        "owner_user_id": "00000000-0000-0000-0000-000000000000",
        "provider": "feishu",
        "title": "未绑定验证",
        "start_time": "2026-09-27T01:00:00Z",
        "end_time": "2026-09-27T02:00:00Z",
        "timezone": "Asia/Shanghai",
    }

    with pytest.raises(CalendarNotBound):
        client.create_calendar_event(payload)
