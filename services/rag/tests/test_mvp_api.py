from __future__ import annotations

import unittest
from unittest.mock import patch

from app.application.rag_service import RetrievalResponse
from app.domain.rag import SearchResult
from app.routers import api
from app.schemas.search import SearchBody


class _Service:
    def search(self, request):
        return RetrievalResponse(
            request_id="request-1",
            results=[SearchResult(
                chunk_id="chunk-1",
                content="content",
                score=1.0,
                rank=1,
                source={
                    "knowledge_item_id": "item-1",
                    "resource_type": "message",
                    "resource_id": "message-1",
                    "content_variant": "display",
                    "rag_eligible": True,
                    "auth_object_key": "must-not-leak",
                },
            )],
            diagnostics={"effective_execution_path": "traditional", "tree_mode": "shadow"},
        )


class ApiTests(unittest.TestCase):
    def test_global_search_adapter_resolves_scope_and_hides_auth_key(self) -> None:
        with patch.object(api, "build_retrieval_service", return_value=_Service()):
            response = api.global_search(
                SearchBody(
                    query="hello",
                    scope_type="organization",
                ),
                x_user_id="user-1",
                x_organization_id="org-1",
            )
        self.assertEqual(response["request_id"], "request-1")
        self.assertEqual(response["items"][0]["knowledge_item_id"], "item-1")
        self.assertNotIn("auth_object_key", response["items"][0])


if __name__ == "__main__":
    unittest.main()
