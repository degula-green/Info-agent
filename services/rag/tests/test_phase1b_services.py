from __future__ import annotations

import unittest

from app.application.entity_review_service import (
    EntityReviewService,
    InvalidReviewIdempotency,
)
from app.application.qa_service import QAService
from app.application.rag_service import RetrievalResponse
from app.domain.rag import Chunk, ResourceContext, SearchRequest, SearchResult
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


class _Retrieval:
    def search(self, request: SearchRequest) -> RetrievalResponse:
        return RetrievalResponse(
            request_id="request-1",
            results=[
                SearchResult(
                    chunk_id="chunk-1",
                    content="source",
                    score=1.0,
                    rank=1,
                    source={
                        "knowledge_item_id": "item-1",
                        "resource_type": "message",
                        "resource_id": "message-1",
                        "source_conversation_id": "conversation-1",
                        "content_variant": "display",
                    },
                )
            ],
            diagnostics={"effective_execution_path": "traditional"},
        )


class _Provider:
    def generate(self, question, results):
        return "answer"

    def generate_stream(self, question, results):
        yield "a"
        yield "b"


class _EmptyRetrieval:
    def search(self, request: SearchRequest) -> RetrievalResponse:
        return RetrievalResponse(
            request_id="request-empty",
            results=[],
            diagnostics={"effective_execution_path": "traditional"},
        )


class _FailingProvider(_Provider):
    def generate(self, question, results):
        raise RuntimeError("provider failed")

    def generate_stream(self, question, results):
        raise RuntimeError("provider failed")
        yield ""


def _request(*, conversation_id: str | None = None) -> SearchRequest:
    return SearchRequest(
        query="question",
        user_id="00000000-0000-0000-0000-000000000001",
        scope_type="organization",
        scope_id="00000000-0000-0000-0000-000000000002",
        conversation_id=conversation_id,
    )


class QAServiceTests(unittest.TestCase):
    def test_answer_persists_user_and_assistant_messages(self) -> None:
        repository = InMemoryRagMVPRepository()
        service = QAService(
            repository=repository,
            retrieval_service=_Retrieval(),
            answer_provider=_Provider(),
        )

        result = service.answer(_request())
        conversation = repository.get_qa_conversation(
            user_id=_request().user_id,
            conversation_id=result["conversation_id"],
        )

        self.assertEqual(result["answer"], "answer")
        self.assertIsNotNone(conversation)
        self.assertEqual(
            [message["role"] for message in conversation["messages"]],
            ["user", "assistant"],
        )

    def test_failed_answer_is_persisted_as_failed(self) -> None:
        repository = InMemoryRagMVPRepository()
        service = QAService(
            repository=repository,
            retrieval_service=_Retrieval(),
            answer_provider=_FailingProvider(),
        )

        with self.assertRaises(RuntimeError):
            service.answer(_request())
        conversation = next(iter(repository.conversations.values()))
        assistant = next(
            message
            for message in repository.messages
            if message["conversation_id"] == conversation["id"]
            and message["role"] == "assistant"
        )
        self.assertEqual(assistant["status"], "failed")

    def test_stream_persists_completed_assistant_message(self) -> None:
        repository = InMemoryRagMVPRepository()
        service = QAService(
            repository=repository,
            retrieval_service=_Retrieval(),
            answer_provider=_Provider(),
        )

        events = list(service.answer_stream(_request()))
        assistant = next(
            message for message in repository.messages if message["role"] == "assistant"
        )

        self.assertEqual(events[-1][0], "done")
        self.assertEqual(assistant["content"], "ab")
        self.assertEqual(assistant["status"], "completed")
        citation = next(payload["citation"] for event, payload in events if event == "citation")
        self.assertEqual(citation["citation_id"], "message:message-1")
        self.assertEqual(citation["type"], "message")
        self.assertEqual(citation["message_id"], "message-1")
        self.assertEqual(citation["conversation_id"], "conversation-1")
        self.assertEqual(citation["snippet"], "source")

    def test_empty_retrieval_does_not_call_provider(self) -> None:
        repository = InMemoryRagMVPRepository()
        service = QAService(
            repository=repository,
            retrieval_service=_EmptyRetrieval(),
            answer_provider=_FailingProvider(),
        )

        result = service.answer(_request())

        self.assertEqual(result["answer"], "未找到足够相关的资料，暂时无法回答。")
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["items"], [])


class EntityReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryRagMVPRepository()
        self.service = EntityReviewService(repository=self.repository)
        context = ResourceContext.from_event_and_source(
            {
                "resource_type": "message",
                "resource_id": "00000000-0000-0000-0000-000000000011",
                "knowledge_item_id": "00000000-0000-0000-0000-000000000012",
                "source_audience_policy": "organization_members",
                "content_version": 1,
                "acl_version": 1,
                "content_hash": "a" * 64,
                "content_access_required": False,
            },
            {
                "scope_type": "organization",
                "scope_id": "00000000-0000-0000-0000-000000000003",
                "knowledge_base_id": "00000000-0000-0000-0000-000000000004",
                "content_hash": "a" * 64,
            },
        )
        snapshot_id = self.repository.upsert_snapshot(context)
        chunk = Chunk.create(
            context=context,
            snapshot_id=snapshot_id,
            chunk_index=0,
            chunk_count=1,
            content="青云项目",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        self.repository.upsert_chunks([chunk])
        self.candidate_id = self.repository.upsert_candidate_mention(
            scope_type="organization",
            scope_id=context.scope_id,
            candidate_name="青云项目",
            normalized_key="青云项目",
            domain="project",
            chunk=chunk,
            context_excerpt=chunk.content,
        )
        self.scope_id = context.scope_id
        self.entity = self.repository.upsert_entity(
            scope_type="organization",
            scope_id=self.scope_id,
            domain="project",
            canonical_name="青云项目正式名称",
            normalized_key="青云项目正式名称",
        )

    def test_review_requires_idempotency_value(self) -> None:
        with self.assertRaises(InvalidReviewIdempotency):
            self.service.review(
                scope_type="organization",
                scope_id=self.scope_id,
                candidate_id=self.candidate_id,
                reviewer_id="00000000-0000-0000-0000-000000000099",
                review_request_id=None,
                idempotency_key=None,
                action="merge",
                expected_status="new",
                canonical_name=None,
                domain=None,
                target_entity_id=self.entity["id"],
                note=None,
            )

    def test_merge_creates_alias_for_target_entity(self) -> None:
        request_id = "00000000-0000-0000-0000-000000000098"
        result = self.service.review(
            scope_type="organization",
            scope_id=self.scope_id,
            candidate_id=self.candidate_id,
            reviewer_id="00000000-0000-0000-0000-000000000099",
            review_request_id=request_id,
            idempotency_key=None,
            action="merge",
            expected_status="new",
            canonical_name=None,
            domain=None,
            target_entity_id=self.entity["id"],
            note=None,
        )

        aliases = [
            alias
            for alias in self.repository.aliases
            if alias["entity_id"] == self.entity["id"]
        ]
        self.assertEqual(result["status"], "merged")
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["display_alias"], "青云项目")

        replay = self.service.review(
            scope_type="organization",
            scope_id=self.scope_id,
            candidate_id=self.candidate_id,
            reviewer_id="00000000-0000-0000-0000-000000000099",
            review_request_id=request_id,
            idempotency_key=None,
            action="merge",
            expected_status="new",
            canonical_name=None,
            domain=None,
            target_entity_id=self.entity["id"],
            note=None,
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(
            len(
                [
                    alias
                    for alias in self.repository.aliases
                    if alias["entity_id"] == self.entity["id"]
                ]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
