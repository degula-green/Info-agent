"""Look form field values up in the internal knowledge base.

The retriever is deliberately a thin adapter over ``knowledge.search_content``
rather than a second RAG client: scoping, protected-content rules and
source grouping already live in that capability, and duplicating them here is
how the two would drift apart.
"""

from __future__ import annotations

from typing import Any, Protocol

DEFAULT_TOP_K = 5


class FormValueRetriever(Protocol):
    def search(self, query: str) -> str:
        """Candidate text for ``query``; empty when nothing is available."""


def texts_from_results(output: dict[str, Any] | None) -> str:
    """Flatten a search_content payload into the text of its chunks."""

    parts: list[str] = []
    for result in (output or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        for chunk in result.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


class KnowledgeFormRetriever:
    """Wraps the registered ``knowledge.search_content`` capability.

    A retrieval outage must degrade the draft (fields stay empty and listed as
    missing), never fail the preview: the owner can still type the values in.
    """

    def __init__(self, capability: Any, *, top_k: int = DEFAULT_TOP_K) -> None:
        self.capability = capability
        self.top_k = max(int(top_k), 1)

    def search(self, query: str) -> str:
        text = str(query or "").strip()
        if not text:
            return ""
        try:
            arguments = self.capability.validate({"query": text, "top_k": self.top_k})
            output = self.capability.execute(arguments)
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return ""
        if not isinstance(output, dict):
            return ""
        return texts_from_results(output)
