"""Look form field values up in the internal knowledge base.

The retriever is deliberately a thin adapter over ``knowledge.search_sources``
and ``knowledge.search_content`` rather than a second RAG client: scoping,
protected-content rules and source grouping already live in those capabilities,
and duplicating them here is how the two would drift apart.

It takes two calls, not one, because a chunk cannot say who it belongs to:

* ``resolve_scope`` turns a subject ("先躺会再说", "我的公司") into the set of
  resources that *are* that subject.
* ``search`` then reads field values strictly inside that set.

A value produced by a scoped search belongs to the scope by construction, which
is the strongest attribution this system can honestly offer. Relying on the
chunk's own metadata is not enough: a group message that happens to mention a
person is stored under the group, not under the person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

DEFAULT_TOP_K = 5
# ``knowledge.search_content`` accepts at most 20 resource ids, so a scope has
# to fit in that window. A subject matching more resources is narrowed by the
# per-field query that runs afterwards.
MAX_SCOPE_RESOURCES = 20


@dataclass(frozen=True)
class RetrievedChunk:
    """One piece of evidence, with the metadata attribution needs."""

    text: str
    resource_id: str = ""
    resource_type: str = ""
    title: str = ""
    sender_name: str = ""
    conversation_name: str = ""
    sent_at: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class ScopeResolution:
    """The resources a subject resolves to, plus their display metadata."""

    scope: str
    resource_ids: tuple[str, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.resource_ids


class FormValueRetriever(Protocol):
    def resolve_scope(self, scope: str) -> ScopeResolution:
        """Resources belonging to ``scope``; empty when the subject is unknown."""

    def search(
        self, query: str, resource_ids: Iterable[str] = ()
    ) -> list[RetrievedChunk]:
        """Evidence for ``query``, restricted to ``resource_ids`` when given."""


def chunks_from_results(output: dict[str, Any] | None) -> list[RetrievedChunk]:
    """Keep each chunk's carrier metadata instead of flattening it away."""

    chunks: list[RetrievedChunk] = []
    for result in (output or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        for chunk in result.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            chunks.append(
                RetrievedChunk(
                    text=text,
                    resource_id=str(result.get("resource_id") or ""),
                    resource_type=str(result.get("resource_type") or ""),
                    title=str(result.get("title") or ""),
                    sender_name=str(result.get("sender_name") or ""),
                    conversation_name=str(result.get("conversation_name") or ""),
                    sent_at=str(result.get("sent_at") or ""),
                    score=float(chunk.get("score") or 0.0),
                )
            )
    return chunks


class KnowledgeFormRetriever:
    """Wraps the registered knowledge capabilities.

    A retrieval outage must degrade the draft (fields stay empty and listed as
    missing), never fail the preview: the owner can still type the values in.
    """

    def __init__(
        self,
        content_capability: Any,
        sources_capability: Any | None = None,
        *,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self.content = content_capability
        self.sources = sources_capability
        self.top_k = max(int(top_k), 1)

    # -- subject resolution -------------------------------------------------

    def resolve_scope(self, scope: str) -> ScopeResolution:
        """Which resources belong to ``scope``.

        Two lookups, because the two ways a subject is filed are different
        things: a company shows up in a document's text, a contact shows up in
        the conversation/sender metadata.
        """

        name = str(scope or "").strip()
        if not name or self.sources is None:
            return ScopeResolution(scope=name)
        collected: list[dict[str, Any]] = []
        resource_ids: list[str] = []
        for arguments in (
            {"query": name},
            {"conversation_names": [name], "sender_names": [name]},
        ):
            for source in self._search_sources(arguments):
                resource_id = str(source.get("resource_id") or "").strip()
                if not resource_id or resource_id in resource_ids:
                    continue
                resource_ids.append(resource_id)
                collected.append(source)
                if len(resource_ids) >= MAX_SCOPE_RESOURCES:
                    break
            if len(resource_ids) >= MAX_SCOPE_RESOURCES:
                break
        return ScopeResolution(
            scope=name,
            resource_ids=tuple(resource_ids),
            sources=tuple(collected),
        )

    def _search_sources(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        payload = {
            **arguments,
            "include_personal": True,
            "top_k": MAX_SCOPE_RESOURCES,
        }
        try:
            output = self.sources.execute(self.sources.validate(payload))
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return []
        if not isinstance(output, dict):
            return []
        sources = output.get("sources")
        if isinstance(sources, list):
            found = [item for item in sources if isinstance(item, dict)]
            if found:
                return found
        # Older/simpler doubles may only report ids.
        return [
            {"resource_id": str(item)}
            for item in (output.get("resource_ids") or [])
            if str(item).strip()
        ]

    # -- value lookup -------------------------------------------------------

    def search(
        self, query: str, resource_ids: Iterable[str] = ()
    ) -> list[RetrievedChunk]:
        text = str(query or "").strip()
        if not text:
            return []
        scoped = [str(item) for item in resource_ids if str(item).strip()]
        payload: dict[str, Any] = {
            "query": text,
            "top_k": self.top_k,
            "include_personal": True,
        }
        if scoped:
            payload["resource_ids"] = scoped[:MAX_SCOPE_RESOURCES]
            payload["restrict_to_resource_ids"] = True
        try:
            output = self.content.execute(self.content.validate(payload))
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return []
        if not isinstance(output, dict):
            return []
        return chunks_from_results(output)


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_SCOPE_RESOURCES",
    "FormValueRetriever",
    "KnowledgeFormRetriever",
    "RetrievedChunk",
    "ScopeResolution",
    "chunks_from_results",
]
