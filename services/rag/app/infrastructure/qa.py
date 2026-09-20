from __future__ import annotations

import json
import urllib.error
import urllib.request
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
        payload = self._payload(question, results, stream=False)
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
        if not settings.qa_stream:
            yield self.generate(question, results)
            return
        payload = self._payload(question, results, stream=True)
        request = urllib.request.Request(
            join_url(settings.qa_api_base_url, "/chat/completions"),
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "text/event-stream", "Content-Type": "application/json", "Authorization": f"Bearer {settings.qa_api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.qa_timeout_seconds) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        payload = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    delta = _extract_delta(payload)
                    if delta:
                        yield delta
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            raise QAUnavailable("QA provider stream failed") from exc

    def _payload(self, question: str, results: list[SearchResult], *, stream: bool) -> dict[str, Any]:
        from app.application.retrieval.context_assembler import assemble_context

        context = assemble_context(results)
        return {
            "model": settings.qa_model,
            "stream": stream,
            "temperature": 0.1,
            "max_tokens": settings.qa_max_output_tokens,
            "messages": [
                {"role": "system", "content": "仅依据给定资料回答。资料不足时明确说明，不要编造。回答中只保留给定的资料编号引用。"},
                {"role": "user", "content": f"问题：{question}\n\n资料：\n{context.text}"},
            ],
        }


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


def _extract_delta(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    return ""
