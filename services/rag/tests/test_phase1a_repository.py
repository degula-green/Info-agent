from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


def _event() -> dict:
    return {
        "event_id": "00000000-0000-0000-0000-000000000020",
        "organization_id": "00000000-0000-0000-0000-000000000003",
        "payload": {
            "resource_type": "message",
            "resource_id": "00000000-0000-0000-0000-000000000011",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000012",
            "source_audience_policy": "organization_members",
            "content_version": 1,
            "acl_version": 1,
        },
    }


class Phase1ARepositoryTests(unittest.TestCase):
    def test_new_job_has_parse_status_and_lease_epoch(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(_event())
        self.assertEqual(job["parse_status"], "pending")
        self.assertEqual(job["lease_epoch"], 0)

    def test_owned_mutations_require_current_owner_and_epoch(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(_event())
        claimed = repository.claim_jobs("parse", limit=1)[0]

        self.assertFalse(
            repository.heartbeat(
                job["id"],
                owner="stale-owner",
                epoch=claimed["lease_epoch"],
            )
        )
        self.assertTrue(
            repository.heartbeat(
                job["id"],
                owner=claimed["lease_owner"],
                epoch=claimed["lease_epoch"],
            )
        )
        self.assertFalse(
            repository.update_job_if_owned(
                job["id"],
                owner="stale-owner",
                epoch=claimed["lease_epoch"],
                fields={"status": "ready"},
            )
        )
        self.assertTrue(
            repository.update_job_if_owned(
                job["id"],
                owner=claimed["lease_owner"],
                epoch=claimed["lease_epoch"],
                fields={"parse_status": "parsed"},
            )
        )

    def test_reclaim_increments_epoch_and_rejects_old_worker(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(_event())
        first = repository.claim_jobs("parse", limit=1)[0]
        repository.jobs[job["id"]]["lease_until"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        second = repository.claim_jobs("parse", limit=1)[0]

        self.assertGreater(second["lease_epoch"], first["lease_epoch"])
        self.assertFalse(
            repository.heartbeat(
                job["id"],
                owner=first["lease_owner"],
                epoch=first["lease_epoch"],
            )
        )


if __name__ == "__main__":
    unittest.main()
