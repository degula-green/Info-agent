from __future__ import annotations

import unittest

from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


class RepositoryTests(unittest.TestCase):
    def test_event_idempotency_and_payload_conflict(self) -> None:
        repository = InMemoryRagMVPRepository()
        base = {
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
        first = repository.create_or_get_job(base)
        second = repository.create_or_get_job(base)
        self.assertEqual(first["id"], second["id"])
        changed = {**base, "payload": {**base["payload"], "content_version": 2}}
        with self.assertRaises(ValueError):
            repository.create_or_get_job(changed)

    def test_outbox_idempotency(self) -> None:
        repository = InMemoryRagMVPRepository()
        event = {
            "event_id": "00000000-0000-0000-0000-000000000011",
            "aggregate_type": "knowledge_item",
            "aggregate_id": "00000000-0000-0000-0000-000000000002",
            "event_type": "knowledge.rag.ready",
            "event_version": 1,
            "payload": {},
        }
        self.assertEqual(repository.add_outbox_event(event), event["event_id"])
        self.assertEqual(len(repository.pending_outbox()), 1)
        repository.add_outbox_event(event)
        self.assertEqual(len(repository.pending_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
