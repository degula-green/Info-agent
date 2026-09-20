from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.application.retrieval.context_assembler import ContextBundle
from app.domain.models import SearchRequest, SearchResult
from app.infrastructure.qa import OpenAICompatibleAnswerProvider
from app.routers import api
from app.services.search_service import PreparedAnswer, SearchResponse


class _Response:
    def __init__(self, lines): self.lines = lines
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def __iter__(self): return iter(self.lines)


class _Provider:
    def generate_stream(self, _question, _results):
        yield "首个"
        yield "增量"


class _Service:
    def __init__(self):
        self.provider = _Provider()
        self.completed = False

    def prepare_answer(self, request):
        return PreparedAnswer(
            request=request, conversation_id="conversation-1", user_message_id="user-1", started=0,
            response=SearchResponse([], SimpleNamespace(), "request-1"), results=[],
            context=ContextBundle("[资料 1] 常见问题.docx\n正文", [{"source_id": "attachment:1", "source_kind": "document", "rank": 1, "file_name": "常见问题.docx"}]),
            diagnostics={}, retrieval_mode="fusion", tree_diagnostics={},
        )

    @property
    def answer_provider(self): return self.provider

    def complete_answer(self, prepared, answer):
        self.completed = True
        return {"assistant_message_id": "assistant-1", "answer": answer, "citations": prepared.context.citations, "diagnostics": {}, "retrieval_mode": "fusion"}

    def fail_answer(self, *_): raise AssertionError("stream should not fail")


class QAStreamingTests(unittest.TestCase):
    def test_openai_compatible_provider_yields_upstream_sse_deltas(self):
        response = _Response([
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":" second"}}]}\n',
            b'data: [DONE]\n',
        ])
        settings = SimpleNamespace(qa_api_base_url="https://provider", qa_api_key="key", qa_model="model", qa_stream=True, qa_timeout_seconds=1, qa_max_output_tokens=100)
        with patch("app.infrastructure.qa.settings", settings), patch("app.infrastructure.qa.urllib.request.urlopen", return_value=response) as open_url:
            values = list(OpenAICompatibleAnswerProvider().generate_stream("question", []))
        self.assertEqual(values, ["first", " second"])
        self.assertIn(b'"stream":true', open_url.call_args.args[0].data)

    def test_rag_sse_sends_sources_before_tokens_and_finishes_after_stream(self):
        service = _Service()
        body = api.AIDocumentBody(query="问题", user_id="user-1")
        with patch.object(api, "get_service", return_value=service):
            response = api.ai_documents_stream(body, x_user_id="user-1")

            async def consume():
                return [item async for item in response.body_iterator]

            events = [item.decode("utf-8") if isinstance(item, bytes) else item for item in asyncio.run(consume())]
        self.assertTrue(events[0].startswith("event: meta"))
        self.assertTrue(events[1].startswith("event: citation"))
        self.assertTrue(events[2].startswith("event: token"))
        self.assertTrue(events[-1].startswith("event: done"))
        self.assertTrue(service.completed)


if __name__ == "__main__":
    unittest.main()
