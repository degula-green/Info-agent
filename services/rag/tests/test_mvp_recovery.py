from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


def _event() -> dict:
    return {
        "event_id": "00000000-0000-0000-0000-000000000010",
        "organization_id": "00000000-0000-0000-0000-000000000003",
        "payload": {
            "resource_type": "message",
            "resource_id": "00000000-0000-0000-0000-000000000001",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000002",
            "source_audience_policy": "organization_members",
            "content_version": 1,
            "acl_version": 1,
        },
    }


class RecoveryTests(unittest.TestCase):
    def test_expired_lease_can_be_recovered(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(_event())
        first = repository.claim_jobs("parse", limit=1)
        self.assertEqual([item["id"] for item in first], [job["id"]])
        self.assertEqual(repository.claim_jobs("parse", limit=1), [])
        repository.jobs[job["id"]]["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        second = repository.claim_jobs("parse", limit=1)
        self.assertEqual([item["id"] for item in second], [job["id"]])

    def test_stage_attempts_are_append_only(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(_event())
        repository.add_attempt(job["id"], lane="parse", stage="parse", status="failed")
        repository.add_attempt(job["id"], lane="parse", stage="parse", status="succeeded")
        self.assertEqual([item["status"] for item in repository.attempts], ["failed", "succeeded"])


if __name__ == "__main__":
    unittest.main()
