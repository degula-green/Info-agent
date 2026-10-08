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

from app.understanding.subject import subject_core, subject_is_organization

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
    conversation_id: str = ""
    conversation_name: str = ""
    sent_at: str = ""
    score: float = 0.0
    # Which of the person's three sources this chunk came from, and whether the
    # subject himself wrote it. Together they decide the attribution tier.
    source_group: str = ""
    speaker_role: str = ""


@dataclass(frozen=True)
class ScopeResolution:
    """The resources a subject resolves to, plus their display metadata."""

    scope: str
    resource_ids: tuple[str, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    # Which of the person's three sources each resource came from ("sent" /
    # "private" / "mention"). Empty when the resolution came from a name
    # search, where the carrier is the only attribution available.
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # The complete, already-exported evidence for each group. When present the
    # caller reads values straight out of it instead of ranking a query inside
    # a truncated id window.
    chunks: tuple[RetrievedChunk, ...] = ()
    # One mention anchor per platform name ("稻成" for WeChat, the Feishu name
    # for Feishu). Empty means there is no name to anchor a mention search on.
    anchors: tuple[str, ...] = ()
    # Names the subject's own messages are filed under, used to tell his words
    # from the other side's inside a private chat.
    subject_names: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.resource_ids


@dataclass(frozen=True)
class PersonScopeResult:
    """A resolved person: the name the corpus knows plus the three groups."""

    subject: str = ""
    sources: tuple[dict[str, Any], ...] = ()
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    chunks: tuple[RetrievedChunk, ...] = ()
    anchors: tuple[str, ...] = ()
    subject_names: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def resource_ids(self) -> tuple[str, ...]:
        collected: list[str] = []
        for _, ids in self.groups:
            collected.extend(ids)
        return tuple(dict.fromkeys(collected))


class PersonScopeResolver(Protocol):
    def resolve(
        self,
        name: str,
        *,
        owner_user_id: str,
        request_id: str = "",
        trace_id: str = "",
        organization_id: str | None = None,
        top_k: int = 10,
    ) -> PersonScopeResult:
        """The three source groups for ``name``; empty groups when unknown."""


class FormValueRetriever(Protocol):
    def resolve_scope(
        self,
        scope: str,
        *,
        owner_user_id: str = "",
        request_id: str = "",
        trace_id: str = "",
        organization_id: str | None = None,
    ) -> ScopeResolution:
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
                    conversation_id=str(result.get("conversation_id") or ""),
                    conversation_name=str(result.get("conversation_name") or ""),
                    sent_at=str(result.get("sent_at") or ""),
                    score=float(chunk.get("score") or 0.0),
                )
            )
    return chunks


def chunks_from_scope_items(
    items: Iterable[dict[str, Any]],
) -> list[RetrievedChunk]:
    """Export items (one per chunk) as retrieval chunks, role labels kept.

    The scope export already decided which of the three sources each chunk
    came from and whether the subject himself wrote it; carrying both through
    is what lets the form tell "his own words" from "the other side's words".
    """

    chunks: list[RetrievedChunk] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        sender = item.get("sender") or {}
        conversation = item.get("conversation") or {}
        chunks.append(
            RetrievedChunk(
                text=text,
                resource_id=str(item.get("resource_id") or ""),
                resource_type=str(item.get("resource_type") or ""),
                title=str(item.get("title") or item.get("file_name") or ""),
                sender_name=str(sender.get("name") or ""),
                conversation_id=str(conversation.get("id") or ""),
                conversation_name=str(conversation.get("name") or ""),
                sent_at=str(item.get("sent_at") or ""),
                source_group=str(item.get("source_group") or ""),
                speaker_role=str(item.get("speaker_role") or ""),
            )
        )
    return chunks


# ``knowledge.search_content`` accepts at most 100 resource ids per call.
MAX_CONTENT_BATCH = 100


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
        person_scope: PersonScopeResolver | None = None,
        context_client: Any | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self.content = content_capability
        self.sources = sources_capability
        self.person_scope = person_scope
        # The neighbour lookup is its own RAG endpoint, so the retriever keeps a
        # client for it rather than pretending it is a content search.
        self.context_client = context_client
        self.top_k = max(int(top_k), 1)

    # -- subject resolution -------------------------------------------------

    def resolve_scope(
        self,
        scope: str,
        *,
        owner_user_id: str = "",
        request_id: str = "",
        trace_id: str = "",
        organization_id: str | None = None,
    ) -> ScopeResolution:
        """Which resources belong to ``scope``.

        A person is read from his three sources -- what he sent, the private
        chat, what mentions him -- because a name search cannot tell his own
        record from a sentence that merely contains his name. A subject that
        reads as an organization (and any deployment without a person resolver)
        keeps the name lookups: a company is filed by the words in its
        documents, never by a conversation or sender.
        """

        name = str(scope or "").strip()
        if not name:
            return ScopeResolution(scope=name)
        if self.person_scope is not None and not subject_is_organization(name):
            person = self.person_scope.resolve(
                name,
                owner_user_id=owner_user_id,
                request_id=request_id,
                trace_id=trace_id,
                organization_id=organization_id,
            )
            if person is None:
                return ScopeResolution(scope=name)
            return ScopeResolution(
                scope=name,
                # The export already bounded this window (per-source caps plus
                # a chunk budget), so it is passed through whole: trimming it to
                # MAX_SCOPE_RESOURCES here is what used to hide most of it.
                resource_ids=tuple(person.resource_ids),
                sources=tuple(person.sources),
                groups=tuple(
                    (group, tuple(ids)) for group, ids in person.groups
                ),
                chunks=tuple(person.chunks),
                anchors=tuple(person.anchors),
                subject_names=tuple(person.subject_names),
                truncated=bool(person.truncated),
            )
        return self._resolve_by_name(name)

    def _resolve_by_name(self, name: str) -> ScopeResolution:
        """Resources filed under the subject's name.

        The body has to carry the full name: BM25 answers "深空公司" and
        "公司" with the same documents, so without that pin a company lookup
        silently widens into everything that mentions the word "公司".
        """

        if self.sources is None:
            return ScopeResolution(scope=name)
        core = subject_core(name) or name
        collected: list[dict[str, Any]] = []
        resource_ids: list[str] = []
        for arguments in (
            {"query": core, "content_contains": [core]},
            {"conversation_names": [core], "sender_names": [core]},
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

    def search_scope(
        self,
        query: str,
        *,
        sender_ids: Iterable[str] = (),
        conversation_ids: Iterable[str] = (),
        content_contains: Iterable[str] = (),
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Hybrid search inside a filter-defined scope, in one call.

        The person's window is expressed as filters -- who sent it, which chat
        it is in, which phrase it carries -- instead of a list of resource ids,
        so one query covers the whole range instead of paging through it.
        """

        text = str(query or "").strip()
        if not text:
            return []
        payload: dict[str, Any] = {
            "query": text,
            "top_k": max(1, int(top_k or self.top_k)),
            "include_personal": True,
        }
        senders = [str(value) for value in sender_ids if str(value).strip()]
        chats = [str(value) for value in conversation_ids if str(value).strip()]
        phrases = [str(value) for value in content_contains if str(value).strip()]
        if senders:
            payload["sender_ids"] = senders[:20]
        if chats:
            payload["conversation_ids"] = chats[:20]
        if phrases:
            payload["content_contains"] = phrases[:10]
        try:
            output = self.content.execute(self.content.validate(payload))
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return []
        if not isinstance(output, dict):
            return []
        return chunks_from_results(output)

    def scope_context(
        self,
        anchors: Iterable[dict[str, Any]],
        *,
        radius: int = 2,
        owner_user_id: str = "",
        organization_id: str | None = None,
        request_id: str = "",
        trace_id: str = "",
    ) -> list[RetrievedChunk]:
        """Messages either side of the anchors, in the same conversation."""

        if self.context_client is None:
            return []
        prepared = [
            {
                "resource_id": str(item.get("resource_id") or ""),
                "conversation_id": str(item.get("conversation_id") or ""),
                "sent_at": str(item.get("sent_at") or ""),
            }
            for item in anchors
            if isinstance(item, dict)
            and str(item.get("resource_id") or "").strip()
            and str(item.get("conversation_id") or "").strip()
            and str(item.get("sent_at") or "").strip()
        ][:50]
        if not prepared:
            return []
        try:
            output = self.context_client.message_context(
                {
                    "anchors": prepared,
                    "radius": max(1, int(radius)),
                    "include_personal": True,
                    "resource_types": ["message"],
                },
                user_id=owner_user_id,
                organization_id=organization_id,
                request_id=request_id,
                trace_id=trace_id,
            )
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return []
        if not isinstance(output, dict):
            return []
        return chunks_from_scope_items(output.get("items") or [])

    def recent_scope_resource_ids(
        self,
        *,
        sender_ids: Iterable[str] = (),
        conversation_ids: Iterable[str] = (),
        content_contains: Iterable[str] = (),
        limit: int = 50,
    ) -> list[str]:
        """The newest resources in one scope part.

        An empty query makes the sources endpoint sort by time, which gives a
        bounded, cheap window for values that carry no field word at all.
        """

        if self.sources is None:
            return []
        payload: dict[str, Any] = {
            "query": "",
            "top_k": max(1, min(int(limit), 50)),
            "include_personal": True,
            "resource_types": ["message", "attachment"],
        }
        senders = [str(value) for value in sender_ids if str(value).strip()]
        chats = [str(value) for value in conversation_ids if str(value).strip()]
        phrases = [str(value) for value in content_contains if str(value).strip()]
        if senders:
            payload["sender_ids"] = senders[:10]
        if chats:
            payload["conversation_ids"] = chats[:10]
        if phrases:
            payload["content_contains"] = phrases[:5]
        try:
            output = self.sources.execute(self.sources.validate(payload))
        except Exception:  # noqa: BLE001 - retrieval is best effort by design
            return []
        if not isinstance(output, dict):
            return []
        return [
            str(value)
            for value in (output.get("resource_ids") or [])
            if str(value).strip()
        ]

    def search(
        self,
        query: str,
        resource_ids: Iterable[str] = (),
        *,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        text = str(query or "").strip()
        if not text:
            return []
        scoped = [str(item) for item in resource_ids if str(item).strip()]
        if not scoped:
            return self._search_content(text, (), top_k)
        # A person's scope is wider than one call accepts; walk it in batches
        # instead of silently dropping everything past the limit.
        batches = [
            tuple(scoped[start:start + MAX_CONTENT_BATCH])
            for start in range(0, len(scoped), MAX_CONTENT_BATCH)
        ]
        chunks: list[RetrievedChunk] = []
        seen: set[str] = set()
        for batch in batches:
            for chunk in self._search_content(text, batch, top_k):
                key = f"{chunk.resource_id}:{chunk.text}"
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(chunk)
        return chunks

    def _search_content(
        self,
        text: str,
        resource_ids: tuple[str, ...],
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        payload: dict[str, Any] = {
            "query": text,
            "top_k": max(1, int(top_k or self.top_k)),
            "include_personal": True,
        }
        if resource_ids:
            payload["resource_ids"] = list(resource_ids)
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
    "PersonScopeResolver",
    "PersonScopeResult",
    "RetrievedChunk",
    "ScopeResolution",
    "chunks_from_results",
    "chunks_from_scope_items",
]
