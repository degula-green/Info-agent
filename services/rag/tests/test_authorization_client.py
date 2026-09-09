from __future__ import annotations

import unittest

from app.domain.models import AccessCheck
from app.infrastructure.service1.authorization_client import Service1AuthorizationClient


class _Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response(self.responses.pop(0))


class AuthorizationClientTests(unittest.TestCase):
    def test_scope_rejects_public_keys_and_wildcards(self):
        http = _Http([{"available": True, "objects": {"knowledge_item": ["knowledge_item:ki-1"]}}])
        client = Service1AuthorizationClient(base_url="http://auth", token="secret", http=http)
        value = client.search_scope(user_id="u", organization_id="o", resource_parts=("original",))
        self.assertFalse(value.available)
        self.assertEqual(http.calls[0][2]["headers"]["X-Caller-Service"], "rag")

    def test_batch_uses_check_ids_and_maps_response(self):
        http = _Http([{"decisions": [
            {"check_id": "c2", "allowed": False},
            {"check_id": "c1", "allowed": True},
        ]}])
        client = Service1AuthorizationClient(base_url="http://auth", token="secret", http=http)
        checks = [
            AccessCheck("knowledge_item", "display", "ki-1"),
            AccessCheck("attachment", "content", "att-1"),
        ]
        self.assertEqual(client.check_batch(user_id="u", organization_id="o", checks=checks), [True, False])
        payload = http.calls[0][2]["body"]
        self.assertEqual([item["check_id"] for item in payload["checks"]], ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
