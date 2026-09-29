from __future__ import annotations

import unittest

from app.domain.rag import AccessCheck
from app.infrastructure.service1.rag_authorization import RagAuthorizationClient


class _Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class _Http:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _Response(next(self.responses))


class AuthorizationTests(unittest.TestCase):
    def test_scope_uses_new_contract(self) -> None:
        http = _Http([{
            "available": True,
            "snapshot_id": "snapshot",
            "expires_at": "2026-09-27T10:00:05Z",
            "authorized_organization_ids": ["org-1"],
            "authorized_conversation_group_ids": ["conversation-1"],
            "authorized_protected_object_keys": ["knowledge_original:item-1"],
            "truncated": False,
        }])
        client = RagAuthorizationClient(base_url="http://core", token="token", http=http)
        scope = client.search_scope(
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            resource_parts=("original", "content"),
        )
        self.assertTrue(scope.available)
        self.assertEqual(scope.authorized_conversation_group_ids, ("conversation-1",))
        self.assertEqual(scope.authorized_protected_object_keys, ("knowledge_original:item-1",))

    def test_truncated_scope_fails_closed(self) -> None:
        http = _Http([{
            "available": True,
            "truncated": True,
            "authorized_protected_object_keys": ["knowledge_original:item-1"],
        }])
        client = RagAuthorizationClient(base_url="http://core", token="token", http=http)
        scope = client.search_scope(
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            resource_parts=("original",),
        )
        self.assertTrue(scope.truncated)

    def test_check_batch_rejects_incomplete_decisions(self) -> None:
        http = _Http([{"decisions": []}])
        client = RagAuthorizationClient(base_url="http://core", token="token", http=http)
        decisions = client.check_batch(
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            checks=[AccessCheck("knowledge_item", "display", "item-1")],
        )
        self.assertEqual(decisions, [False])


if __name__ == "__main__":
    unittest.main()
