from __future__ import annotations

import json
from typing import Any, Iterable

from app.application.ports import AnswerProvider
from app.config import settings
from app.domain.models import SearchResult
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class QAUnavailable(RuntimeError):
    pass


class OpenAICompatibleAnswerProvider(AnswerProvider):
    """Small adapter for OpenAI-compatible chat completion endpoints."""

    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def generate(self, question: str, results: list[SearchResult]) -> str:
        if not settings.qa_api_base_url or not settings.qa_api_key or not settings.qa_model:
            raise QAUnavailable("QA provider is not configured")
        from app.application.retrieval.context_assembler import assemble_context

        context = assemble_context(results)
        payload = {
            "model": settings.qa_model,
            "stream": False,
            "temperature": 0.1,
            "max_tokens": settings.qa_max_output_tokens,
            "messages": [
                {"role": "system", "content": "仅依据给定资料回答。资料不足时明确说明，不要编造。回答中保留资料编号引用。"},
                {"role": "user", "content": f"问题：{question}\n\n资料：\n{context.text}"},
            ],
        }
        try:
            value = self.http.request(
                "POST", join_url(settings.qa_api_base_url, "/chat/completions"),
                body=payload, token=settings.qa_api_key, timeout=settings.qa_timeout_seconds,
            ).json()
        except IntegrationError as exc:
            raise QAUnavailable("QA provider request failed") from exc
        text = _extract_text(value)
        if not text:
            raise QAUnavailable("QA provider returned an empty answer")
        return text

    def generate_stream(self, question: str, results: list[SearchResult]):
        # The stdlib HTTP adapter intentionally exposes a single response. The
        # API can still stream one final chunk until a streaming transport is
        # introduced; this keeps the port deterministic for tests.
        yield self.generate(question, results)


def _extract_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"].strip()
    if isinstance(first.get("text"), str):
        return first["text"].strip()
    return ""
