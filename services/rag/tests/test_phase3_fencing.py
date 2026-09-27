from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.application.runtime import LeaseLost, MVPWorkerRuntime
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


class _Callbacks:
    def flush(self, *, limit=50):
        return 0


class Phase3FencingTests(unittest.TestCase):
    def test_old_worker_cannot_write_after_lease_is_stolen(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(
            {
                "event_id": "00000000-0000-0000-0000-000000000050",
                "organization_id": "00000000-0000-0000-0000-000000000003",
                "payload": {
                    "resource_type": "message",
                    "resource_id": "00000000-0000-0000-0000-000000000051",
                    "knowledge_item_id": "00000000-0000-0000-0000-000000000052",
                    "source_audience_policy": "organization_members",
                    "content_version": 1,
                    "acl_version": 1,
                },
            }
        )
        first = repository.claim_jobs("parse", limit=1, job_id=job["id"])[0]
        repository.jobs[job["id"]]["lease_until"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        second = repository.claim_jobs("parse", limit=1, job_id=job["id"])[0]
        self.assertGreater(second["lease_epoch"], first["lease_epoch"])

        runtime = MVPWorkerRuntime(
            repository=repository,
            parse_service=object(),
            index_service=object(),
            memory_service=object(),
            callback_lane=_Callbacks(),
        )
        with self.assertRaises(LeaseLost):
            runtime._owned_update(first, status="failed")


if __name__ == "__main__":
    unittest.main()
