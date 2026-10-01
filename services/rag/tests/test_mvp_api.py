from __future__ import annotations

import unittest

from app.application.rag_service import RetrievalResponse
from app.config import settings
from app.domain.rag import SearchResult
from app.routers import api
from app.schemas.search import ContentSearchBody, SearchBody, SourceSearchBody


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
                    "sender_identity_id": "sender-1",
                    "sender_display_name": "张三",
                    "source_conversation_id": "conversation-1",
                    "source_conversation_name": "财务群",
                    "source_conversation_type": "group",
                    "source_platform": "feishu",
                    "sent_at": "2026-10-01T10:30:00+08:00",
                    "content_variant": "display",
                    "rag_eligible": True,
                    "auth_object_key": "must-not-leak",
                },
            )],
            diagnostics={"effective_execution_path": "traditional", "tree_mode": "shadow"},
        )


class ApiTests(unittest.TestCase):
    def test_global_search_adapter_resolves_scope_and_hides_auth_key(self) -> None:
        response = api.global_search(
            SearchBody(
                query="hello",
                scope_type="organization",
            ),
            x_user_id="user-1",
            x_organization_id="org-1",
            service=_Service(),
        )
        self.assertEqual(response["request_id"], "request-1")
        self.assertEqual(response["items"][0]["knowledge_item_id"], "item-1")
        self.assertNotIn("auth_object_key", response["items"][0])

    def test_agent_source_search_maps_metadata_and_requires_token(self) -> None:
        response = api.search_sources(
            SourceSearchBody(query="", scope_type="organization"),
            x_user_id="user-1",
            x_organization_id="org-1",
            x_agent_service_token="",
            service=_Service(),
        )
        self.assertEqual(response["resource_ids"], ["message-1"])
        self.assertEqual(response["items"][0]["sender"]["name"], "张三")
        self.assertEqual(response["items"][0]["conversation"]["name"], "财务群")
        self.assertEqual(response["diagnostics"]["metadata_coverage"], "complete")

    def test_agent_content_search_returns_grouped_chunks(self) -> None:
        response = api.search_content(
            ContentSearchBody(query="预算", scope_type="organization"),
            x_user_id="user-1",
            x_organization_id="org-1",
            x_agent_service_token="",
            service=_Service(),
        )
        self.assertEqual(response["returned_source_count"], 1)
        self.assertEqual(response["items"][0]["matched_chunk_count"], 1)
        self.assertEqual(response["items"][0]["chunks"][0]["text"], "content")

    def test_agent_search_rejects_wrong_service_token(self) -> None:
        original = settings.agent_service_token
        object.__setattr__(settings, "agent_service_token", "expected")
        try:
            with self.assertRaises(api.HTTPException) as raised:
                api.search_sources(
                    SourceSearchBody(query="", scope_type="organization"),
                    x_user_id="user-1",
                    x_organization_id="org-1",
                    x_agent_service_token="wrong",
                    service=_Service(),
                )
            self.assertEqual(raised.exception.status_code, 401)
        finally:
            object.__setattr__(settings, "agent_service_token", original)


if __name__ == "__main__":
    unittest.main()
