from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.domain.memory import FactCandidate, SourceRouteCandidate
from app.domain.models import ChunkRecord
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class MemoryModelUnavailable(RuntimeError):
    retryable = True


class OpenAICompatibleFactExtractor:
    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()
        self._last_routes: list[SourceRouteCandidate] = []

    def extract(self, chunks: list[ChunkRecord]) -> list[FactCandidate]:
        if not chunks:
            return []
        if not settings.qa_api_base_url or not settings.qa_api_key or not settings.qa_model:
            raise MemoryModelUnavailable("fact extraction model is not configured")
        material = "\n".join(f"[{i}] {chunk.content}" for i, chunk in enumerate(chunks))
        payload = {
            "model": settings.qa_model,
            "stream": False,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": (
                    "从资料中抽取可独立引用的企业事实。只输出 JSON："
                    '{"facts":[{"fact_type":"event|state|task|relation","text":"...",'
                    '"subject":"...","entity_type":"project|person|system|contract|organization|unknown",'
                    '"predicate":"...","normalized_value":{},"topic":"general",'
                    '"phase":"general","occurred_at":null,"confidence":0.0,"chunk_indexes":[0]}],'
                    '"source_routes":[{"subject":"...","entity_type":"organization|project|person|department|system|contract|unknown",'
                    '"topic":"general","phase":"general","relation_type":"primary|evidence|reference|policy","confidence":0.0}]}。'
                    "即使没有可抽取事实，也要根据资料主体和主题输出 source_routes；不得猜测。"
                )},
                {"role": "user", "content": material},
            ],
        }
        try:
            response = self.http.request(
                "POST", join_url(settings.qa_api_base_url, "/chat/completions"),
                body=payload, token=settings.qa_api_key, timeout=settings.qa_timeout_seconds,
            ).json()
        except IntegrationError as exc:
            raise MemoryModelUnavailable("fact extraction request failed") from exc
        facts = parse_fact_response(response, chunks)
        self._last_routes = parse_source_routes(response)
        return facts

    def source_routes(self, _chunks: list[ChunkRecord]) -> list[SourceRouteCandidate]:
        return list(self._last_routes)


class OpenAICompatibleNodeSummarizer:
    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def summarize(self, *, title: str, facts: list[str]) -> str:
        if not facts:
            return title
        if not settings.qa_api_base_url or not settings.qa_api_key or not settings.qa_model:
            raise MemoryModelUnavailable("summary model is not configured")
        payload = {
            "model": settings.qa_model, "stream": False, "temperature": 0,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": "仅根据事实生成一段简洁、无推测的中文摘要，不输出标题或列表。"},
                {"role": "user", "content": f"节点：{title}\n事实：\n" + "\n".join(f"- {x}" for x in facts)},
            ],
        }
        try:
            value = self.http.request(
                "POST", join_url(settings.qa_api_base_url, "/chat/completions"),
                body=payload, token=settings.qa_api_key, timeout=settings.qa_timeout_seconds,
            ).json()
        except IntegrationError as exc:
            raise MemoryModelUnavailable("summary request failed") from exc
        text = _choice_text(value)
        if not text:
            raise MemoryModelUnavailable("summary model returned empty output")
        return text


class DeterministicFactExtractor:
    """Explicit test/fixture double; production bootstrap never selects it."""
    def __init__(self, facts: list[FactCandidate]) -> None:
        self.facts = list(facts)

    def extract(self, chunks: list[ChunkRecord]) -> list[FactCandidate]:
        return list(self.facts)


class DeterministicNodeSummarizer:
    def summarize(self, *, title: str, facts: list[str]) -> str:
        return f"{title}：" + "；".join(facts)


def parse_fact_response(response: Any, chunks: list[ChunkRecord]) -> list[FactCandidate]:
    text = _choice_text(response)
    if not text:
        raise MemoryModelUnavailable("fact extraction model returned empty output")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MemoryModelUnavailable("fact extraction output is not valid JSON") from exc
    rows = value.get("facts") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise MemoryModelUnavailable("fact extraction output has no facts array")
    output: list[FactCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            raise MemoryModelUnavailable("fact extraction output contains invalid fact")
        indexes = row.get("chunk_indexes") or []
        if not isinstance(indexes, list) or any(not isinstance(i, int) or i < 0 or i >= len(chunks) for i in indexes):
            raise MemoryModelUnavailable("fact extraction output contains invalid chunk indexes")
        try:
            output.append(FactCandidate(
                fact_type=str(row.get("fact_type") or ""), text=str(row.get("text") or ""),
                subject=str(row.get("subject") or ""), entity_type=str(row.get("entity_type") or "unknown"),
                predicate=str(row.get("predicate") or "related_to"),
                normalized_value=dict(row.get("normalized_value") or {}),
                topic=str(row.get("topic") or "general"), phase=str(row.get("phase") or "general"),
                occurred_at=str(row["occurred_at"]) if row.get("occurred_at") else None,
                confidence=float(row.get("confidence", 1)),
                chunk_ids=tuple(chunks[i].chunk_id for i in indexes),
            ))
        except (TypeError, ValueError) as exc:
            raise MemoryModelUnavailable("fact extraction output failed validation") from exc
    return output


def parse_source_routes(response: Any) -> list[SourceRouteCandidate]:
    text = _choice_text(response)
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = value.get("source_routes") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return []
    output: list[SourceRouteCandidate] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("subject") or "").strip():
            continue
        try:
            output.append(SourceRouteCandidate(
                subject=str(row["subject"]), entity_type=str(row.get("entity_type") or "unknown"),
                topic=str(row.get("topic") or "general"), phase=str(row.get("phase") or "general"),
                relation_type=str(row.get("relation_type") or "reference"),
                confidence=float(row.get("confidence", 1)),
            ))
        except (TypeError, ValueError):
            continue
    return output


def _choice_text(value: Any) -> str:
    choices = value.get("choices") if isinstance(value, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"].strip()
    return ""
