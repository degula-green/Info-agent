from __future__ import annotations

import unittest

from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


def _event(*, payload_scope=None, audience="owner_only") -> dict:
    payload = {
        "resource_type": "message",
        "resource_id": "00000000-0000-0000-0000-000000000001",
        "knowledge_item_id": "00000000-0000-0000-0000-000000000002",
        "source_audience_policy": audience,
        "content_version": 1,
        "acl_version": 1,
    }
    if payload_scope:
        payload.update(payload_scope)
    return {
        "event_id": "00000000-0000-0000-0000-000000000030",
        "organization_id": "",
        "payload": payload,
    }


class AuthoritativeScopeTests(unittest.TestCase):
    def test_owner_only_requires_owner_user_id(self) -> None:
        repository = InMemoryRagMVPRepository()
        with self.assertRaisesRegex(ValueError, "owner_user_id"):
            repository.create_or_get_job(_event())

    def test_explicit_scope_is_authoritative(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = repository.create_or_get_job(
            _event(
                payload_scope={
                    "scope_type": "user",
                    "scope_id": "00000000-0000-0000-0000-000000000040",
                    "owner_user_id": "00000000-0000-0000-0000-000000000040",
                    "knowledge_scope": "private",
                }
            )
        )
        self.assertEqual(job["scope_type"], "user")
        self.assertEqual(
            job["scope_id"],
            "00000000-0000-0000-0000-000000000040",
        )

    def test_explicit_scope_cannot_disagree_with_metadata(self) -> None:
        repository = InMemoryRagMVPRepository()
        with self.assertRaisesRegex(ValueError, "mismatches scope_id"):
            repository.create_or_get_job(
                _event(
                    payload_scope={
                        "scope_type": "organization",
                        "scope_id": "00000000-0000-0000-0000-000000000040",
                        "organization_id": "00000000-0000-0000-0000-000000000041",
                    },
                    audience="organization_members",
                )
            )


if __name__ == "__main__":
    unittest.main()
