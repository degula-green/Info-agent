"""Shared access to one person's archive (his messages + private chat + mentions).

``person.query`` answers a free-form question; ``report.weekly`` fills a
template. Both must look at exactly the same three sources, so the scoping
lives here and each capability only decides what to do with the evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.capabilities.knowledge import (
    _merge_content,
    _parallel_scope_calls,
    _scope_requests,
)
from app.infrastructure.knowledge.client import KnowledgeClient, KnowledgeUnavailable
from app.infrastructure.rag.client import RAGClient

SOURCE_SENT = "sent"
SOURCE_PRIVATE = "private"
SOURCE_MENTION = "mention"

# One page per call, and every call pays an authorization lookup (~2s with the
# remote store). A bigger page keeps the whole person window to a handful of
# calls instead of dozens.
DEFAULT_SCOPE_PAGE_SIZE = 200
DEFAULT_MAX_SCOPE_CHUNKS = 5000
# A person's resource window is bounded so one chatty archive cannot turn a
# preview into an unbounded crawl. Reaching the cap is reported, never silent.
DEFAULT_MAX_SCOPE_RESOURCES = 2000
# ``knowledge.search_content`` (and the RAG endpoint behind it) accepts at most
# 100 resource ids per call.
CONTENT_RESOURCE_BATCH = 100


class PersonScopeUnavailable(RuntimeError):
    classification = "retryable_error"


def resolve_person(
    knowledge: KnowledgeClient,
    *,
    owner_user_id: str,
    name: str,
    request_id: str = "",
    trace_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Return (subject_name, matches) for a name the user typed."""

    try:
        resolved = knowledge.resolve_person(
            owner_user_id=owner_user_id,
            name=name,
            request_id=request_id,
            trace_id=trace_id,
        )
    except KnowledgeUnavailable as exc:
        raise PersonScopeUnavailable("person lookup is unavailable") from exc
    subject = str(resolved.get("subject_name") or "").strip()
    matches = [
        item for item in (resolved.get("matches") or []) if isinstance(item, dict)
    ]
    return subject, matches


def identity_ids_of(match: dict[str, Any]) -> list[str]:
    return [str(v) for v in (match.get("identity_ids") or []) if str(v).strip()]


def private_conversation_ids_of(match: dict[str, Any]) -> list[str]:
    return [
        str(v)
        for v in (match.get("private_conversation_ids") or [])
        if str(v).strip()
    ]


# Account labels, not names anybody writes in a message. Anchoring a mention
# search on one matches nothing, so they are skipped when picking anchors.
_ACCOUNT_LABEL = re.compile(r"wxid_|ou_|^用户\d+$", re.IGNORECASE)
_SELF_REFERENCE_LABEL = re.compile(
    r"^(?:我|我的|我自己|本人|自己|我们|咱们)$"
)


def usable_person_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return not _ACCOUNT_LABEL.search(text) and not _SELF_REFERENCE_LABEL.fullmatch(
        text
    )


def identities_of(match: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (match.get("identities") or [])
        if isinstance(item, dict)
    ]


def identity_names_by_platform(match: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """The name each platform actually shows for this person."""

    names: dict[str, list[str]] = {}
    for item in identities_of(match):
        platform = str(item.get("platform") or "").strip().lower()
        name = str(item.get("display_name") or "").strip()
        if not platform or not usable_person_name(name):
            continue
        bucket = names.setdefault(platform, [])
        if name not in bucket:
            bucket.append(name)
    return {platform: tuple(values) for platform, values in names.items()}


def mention_anchors(match: dict[str, Any], *, fallback: str = "") -> tuple[str, ...]:
    """One anchor per platform name, in a stable order.

    A mention search matches text others wrote, so the anchor has to be the
    name that platform shows. When no usable name is known the anchor list is
    empty and the caller must skip the mention source rather than search for a
    pronoun that matches everything.
    """

    by_platform = identity_names_by_platform(match)
    ordered = [
        *by_platform.get("wechat", ()),
        *by_platform.get("feishu", ()),
    ]
    for platform in sorted(by_platform):
        for name in by_platform[platform]:
            if name not in ordered:
                ordered.append(name)
    if not ordered and usable_person_name(fallback):
        ordered.append(str(fallback).strip())
    return tuple(dict.fromkeys(ordered))


def subject_sender_names(match: dict[str, Any]) -> tuple[str, ...]:
    """Names the subject's own messages are filed under.

    Used to tell "the subject said it" from "the other side said it" inside a
    private chat, where both parties' messages live in the same conversation.
    """

    names: list[str] = []
    for item in identities_of(match):
        name = str(item.get("display_name") or "").strip()
        if name:
            names.append(name)
    display = str(match.get("display_name") or "").strip()
    if display:
        names.append(display)
    return tuple(dict.fromkeys(names))


def relation_ids_of(match: dict[str, Any]) -> list[str]:
    return [
        str(v)
        for v in (match.get("relation_ids") or [])
        if str(v).strip()
    ]


def _source_bodies(
    *,
    identity_ids: Iterable[str],
    private_ids: Iterable[str],
    subject: str,
    source_scope: Iterable[str] = (),
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    private_occurred_after: str | None = None,
    private_occurred_before: str | None = None,
    mention_anchors: Iterable[str] | None = None,
    resource_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    wanted = {value for value in source_scope if value}
    types = resource_types or ["message", "attachment"]
    window: dict[str, Any] = {}
    if occurred_after:
        window["occurred_after"] = occurred_after
    if occurred_before:
        window["occurred_before"] = occurred_before
    bodies: list[dict[str, Any]] = []
    sent_ids = [str(v) for v in identity_ids if str(v).strip()]
    private_ids = [str(v) for v in private_ids if str(v).strip()]
    if (not wanted or SOURCE_SENT in wanted) and sent_ids:
        bodies.append(
            {**window, "sender_ids": sent_ids, "resource_types": types}
        )
    if (not wanted or SOURCE_PRIVATE in wanted) and private_ids:
        private_window = dict(window)
        if private_occurred_after:
            private_window["occurred_after"] = private_occurred_after
        if private_occurred_before:
            private_window["occurred_before"] = private_occurred_before
        bodies.append(
            {
                **private_window,
                "conversation_ids": private_ids,
                "resource_types": types,
            }
        )
    if not wanted or SOURCE_MENTION in wanted:
        anchors = (
            tuple(mention_anchors)
            if mention_anchors is not None
            else (subject,)
        )
        for anchor in dict.fromkeys(str(value or "").strip() for value in anchors):
            if not anchor or not usable_person_name(anchor):
                continue
            bodies.append(
                {**window, "content_contains": [anchor], "resource_types": types}
            )
    return bodies


def _source_body_entries(
    *,
    identity_ids: Iterable[str],
    private_ids: Iterable[str],
    subject: str,
    source_scope: Iterable[str] = (),
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    private_occurred_after: str | None = None,
    private_occurred_before: str | None = None,
    mention_anchors: Iterable[str] | None = None,
    resource_types: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    wanted = {value for value in source_scope if value}
    types = resource_types or ["message", "attachment"]
    window: dict[str, Any] = {}
    if occurred_after:
        window["occurred_after"] = occurred_after
    if occurred_before:
        window["occurred_before"] = occurred_before
    entries: list[tuple[str, dict[str, Any]]] = []
    sent_ids = [str(v) for v in identity_ids if str(v).strip()]
    private_ids = [str(v) for v in private_ids if str(v).strip()]
    if (not wanted or SOURCE_SENT in wanted) and sent_ids:
        entries.append(
            (SOURCE_SENT, {**window, "sender_ids": sent_ids, "resource_types": types})
        )
    if (not wanted or SOURCE_PRIVATE in wanted) and private_ids:
        private_window = dict(window)
        if private_occurred_after:
            private_window["occurred_after"] = private_occurred_after
        if private_occurred_before:
            private_window["occurred_before"] = private_occurred_before
        entries.append(
            (
                SOURCE_PRIVATE,
                {
                    **private_window,
                    "conversation_ids": private_ids,
                    "resource_types": types,
                },
            )
        )
    if not wanted or SOURCE_MENTION in wanted:
        anchors = (
            tuple(mention_anchors)
            if mention_anchors is not None
            else (subject,)
        )
        for anchor in dict.fromkeys(str(value or "").strip() for value in anchors):
            if not anchor or not usable_person_name(anchor):
                continue
            entries.append(
                (
                    SOURCE_MENTION,
                    {
                        **window,
                        "content_contains": [anchor],
                        "resource_types": types,
                    },
                )
            )
    return entries


@dataclass
class PersonScopeExport:
    chunks: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    truncated: bool = False


def export_person_scope(
    rag: RAGClient,
    *,
    owner_user_id: str,
    organization_id: str | None,
    request_id: str,
    trace_id: str,
    identity_ids: Iterable[str],
    private_ids: Iterable[str],
    subject: str,
    source_scope: Iterable[str] = (),
    page_size: int = DEFAULT_SCOPE_PAGE_SIZE,
    max_chunks: int = DEFAULT_MAX_SCOPE_CHUNKS,
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    private_occurred_after: str | None = None,
    private_occurred_before: str | None = None,
    mention_anchors: Iterable[str] | None = None,
    resource_types: list[str] | None = None,
) -> PersonScopeExport:
    """Export every visible chunk in the person's three source families.

    The result is complete unless ``truncated`` is set. Pages are requested
    with a stable (sent_at, chunk_id) order, so a weak natural-language query
    can no longer hide the one message that contains a phone number or a
    student id.
    """

    subject_ids = {str(value) for value in identity_ids if str(value).strip()}
    entries = _source_body_entries(
        identity_ids=subject_ids,
        private_ids=private_ids,
        subject=subject,
        source_scope=source_scope,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        private_occurred_after=private_occurred_after,
        private_occurred_before=private_occurred_before,
        mention_anchors=mention_anchors,
        resource_types=resource_types,
    )
    scopes = _scope_requests(
        owner_user_id=owner_user_id,
        organization_id=organization_id,
        include_personal=True,
    )
    export = PersonScopeExport()
    seen_chunks: set[str] = set()
    for source_name, body in entries:
        types = list(body.get("resource_types") or ["message", "attachment"])
        for resource_type in types:
            for scope in scopes:
                coverage_key = f"{source_name}:{resource_type}:{scope['scope_type']}"
                counter = export.coverage.setdefault(
                    coverage_key,
                    {"pages": 0, "returned": 0, "truncated": False},
                )
                offset = 0
                expected_more = False
                while True:
                    request_body = {
                        **body,
                        **scope,
                        "query": "",
                        "resource_types": [resource_type],
                        "top_k": max(1, min(int(page_size), 200)),
                        "offset": offset,
                        "include_protected": True,
                    }
                    # A transient failure -- or an empty page where the previous
                    # one promised more -- must not read as "no more data":
                    # either one silently truncated the window and the form came
                    # back missing fields it used to fill. Retry once, then
                    # record the gap instead of hiding it.
                    items: list[dict[str, Any]] = []
                    response: dict[str, Any] | None = None
                    for attempt in range(2):
                        try:
                            response = rag.export_scope(
                                request_body,
                                user_id=owner_user_id,
                                organization_id=organization_id,
                                request_id=request_id,
                                trace_id=trace_id,
                            )
                        except Exception:  # noqa: BLE001 - retry, then degrade
                            response = None
                        if response is not None:
                            items = [
                                item
                                for item in (response.get("items") or [])
                                if isinstance(item, dict)
                            ]
                            if items or not expected_more:
                                break
                        if attempt == 1:
                            counter["truncated"] = True
                            export.truncated = True
                            break
                    if response is None or (not items and expected_more):
                        break
                    counter["pages"] += 1
                    for item in items:
                        chunk_id = str(item.get("chunk_id") or "")
                        if not chunk_id or chunk_id in seen_chunks:
                            continue
                        seen_chunks.add(chunk_id)
                        sender = item.get("sender") or {}
                        sender_id = str(sender.get("id") or "")
                        if sender_id and sender_id in subject_ids:
                            attribution = "subject_said"
                            speaker_role = "subject"
                        elif source_name == SOURCE_MENTION:
                            attribution = "mentioned"
                            speaker_role = "other"
                        else:
                            attribution = "other_said"
                            speaker_role = "other"
                        item["source_group"] = source_name
                        item["speaker_role"] = speaker_role
                        item["attribution"] = attribution
                        export.chunks.append(item)
                        counter["returned"] += 1
                        if len(export.chunks) >= max(1, int(max_chunks)):
                            counter["truncated"] = True
                            export.truncated = True
                            break
                    if counter["truncated"]:
                        break
                    has_more = bool(response.get("has_more"))
                    expected_more = has_more
                    next_offset = int(
                        response.get("next_offset")
                        or (offset + len(items))
                    )
                    if not items or not has_more or next_offset <= offset:
                        break
                    offset = next_offset
                if counter["truncated"]:
                    break
            if export.truncated:
                break
        if export.truncated:
            break
    return export


def _paged_call(call, body: dict[str, Any], **identity: Any) -> dict[str, Any]:
    """One retry for a transient retrieval outage.

    A failed page used to read as "no more data" and silently truncated the
    person's window, which showed up as a form missing fields it normally
    fills. A second failure propagates so the caller can report the gap.
    """

    try:
        return call(body, **identity)
    except Exception:  # noqa: BLE001 - retry once, then let it surface
        return call(body, **identity)


def collect_resource_ids(
    rag: RAGClient,
    *,
    owner_user_id: str,
    organization_id: str | None,
    request_id: str,
    trace_id: str,
    identity_ids: Iterable[str],
    private_ids: Iterable[str],
    subject: str,
    source_scope: Iterable[str] = (),
    top_k: int = 10,
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    private_occurred_after: str | None = None,
    private_occurred_before: str | None = None,
    mention_anchors: Iterable[str] | None = None,
    resource_types: list[str] | None = None,
) -> list[str]:
    """Every resource id that belongs to the person's three sources.

    Pages through the scope export instead of taking whatever the newest page
    happened to hold. The old single call was silently capped, which is why a
    value three hundred messages back was invisible, and why allowing
    attachments could push the messages out of the window entirely.
    """

    resource_ids: list[str] = []
    for source_name, body in _source_body_entries(
        identity_ids=identity_ids,
        private_ids=private_ids,
        subject=subject,
        source_scope=source_scope,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        private_occurred_after=private_occurred_after,
        private_occurred_before=private_occurred_before,
        mention_anchors=mention_anchors,
        resource_types=resource_types,
    ):
        for resource_type in list(body.get("resource_types") or ["message"]):
            for scope in _scope_requests(
                owner_user_id=owner_user_id,
                organization_id=organization_id,
                include_personal=True,
            ):
                offset = 0
                while True:
                    request_body = {
                        **body,
                        **scope,
                        "query": "",
                        "resource_types": [resource_type],
                        # Page size, not the caller's result budget: a small
                        # page here multiplies the authorization cost.
                        "top_k": max(
                            1, min(max(int(top_k), DEFAULT_SCOPE_PAGE_SIZE), 200)
                        ),
                        "offset": offset,
                        "include_protected": True,
                    }
                    export = getattr(rag, "export_scope", None)
                    if callable(export):
                        response = _paged_call(
                            export,
                            request_body,
                            user_id=owner_user_id,
                            organization_id=organization_id,
                            request_id=request_id,
                            trace_id=trace_id,
                        )
                    else:
                        # Older adapters (and lightweight test doubles) only
                        # expose the legacy resource-id lookup. Keep them
                        # working; they simply cannot page beyond one batch.
                        response = _paged_call(
                            rag.search_sources,
                            request_body,
                            user_id=owner_user_id,
                            organization_id=organization_id,
                            request_id=request_id,
                            trace_id=trace_id,
                        )
                    items = [
                        item
                        for item in (response.get("items") or [])
                        if isinstance(item, dict)
                    ]
                    if not items and response.get("resource_ids"):
                        resource_ids.extend(
                            str(value)
                            for value in response.get("resource_ids") or []
                            if str(value).strip()
                        )
                        break
                    for item in items:
                        value = str(item.get("resource_id") or "").strip()
                        if value:
                            resource_ids.append(value)
                    if len(resource_ids) >= DEFAULT_MAX_SCOPE_RESOURCES:
                        return list(dict.fromkeys(resource_ids))
                    if not items or not bool(response.get("has_more")):
                        break
                    next_offset = int(
                        response.get("next_offset") or (offset + len(items))
                    )
                    if next_offset <= offset:
                        break
                    offset = next_offset
    return list(dict.fromkeys(resource_ids))


def search_content(
    rag: RAGClient,
    *,
    owner_user_id: str,
    organization_id: str | None,
    request_id: str,
    trace_id: str,
    resource_ids: list[str],
    question: str,
    name: str,
    subject: str,
    top_k: int = 10,
):
    """Rank the question inside the person's window.

    The window is wider than one call accepts, so it is walked in batches and
    the ranked results are merged. Passing the whole window in one body would
    be rejected at 100 ids and, before that, silently truncated.
    """

    scopes = _scope_requests(
        owner_user_id=owner_user_id,
        organization_id=organization_id,
        include_personal=True,
    )
    query = strip_anchor(question, name, subject)
    window = [str(value) for value in resource_ids if str(value).strip()]
    batches = (
        [
            window[start:start + CONTENT_RESOURCE_BATCH]
            for start in range(0, len(window), CONTENT_RESOURCE_BATCH)
        ]
        if window
        else [[]]
    )
    raw_responses: list[dict[str, Any]] = []
    for batch in batches:
        body = {
            "query": query,
            "resource_ids": batch,
            "restrict_to_resource_ids": bool(batch),
            "top_k": top_k,
            "include_protected": True,
            "group_by_source": True,
        }
        raw_responses.extend(
            _parallel_scope_calls(
            scopes,
            lambda scope, body=body: rag.search_content(
                {**body, **scope},
                user_id=owner_user_id,
                organization_id=organization_id,
                request_id=request_id,
                trace_id=trace_id,
            ),
            )
        )
    return _merge_content(raw_responses, limit=top_k, min_score=0.0)


def strip_anchor(question: str, name: str, subject: str) -> str:
    """Remove the person anchor so retrieval ranks on the actual question."""

    value = str(question or "")
    for token in {str(name or "").strip(), str(subject or "").strip()}:
        if token:
            value = value.replace(token, " ")
    return " ".join(value.split()) or str(question or "")


class PersonResourceScope:
    """A name in, the person's three source groups out.

    ``person.query`` and ``report.weekly`` only need the flat union of the three
    sources. Form filling also has to know *which* source a value came from --
    "he sent it" is proof of ownership, "someone mentioned him" is not -- so
    this adapter keeps the groups separate instead of flattening them.
    """

    def __init__(self, rag: RAGClient, knowledge: KnowledgeClient) -> None:
        self.rag = rag
        self.knowledge = knowledge

    def resolve(
        self,
        name: str,
        *,
        owner_user_id: str,
        request_id: str = "",
        trace_id: str = "",
        organization_id: str | None = None,
        top_k: int = 10,
    ):
        from app.infrastructure.rag.form_retriever import (
            PersonScopeResult,
            chunks_from_scope_items,
        )

        subject, matches = resolve_person(
            self.knowledge,
            owner_user_id=owner_user_id,
            name=name,
            request_id=request_id,
            trace_id=trace_id,
        )
        if not matches:
            return PersonScopeResult(subject=subject or name)
        identity_ids: list[str] = []
        private_ids: list[str] = []
        for match in matches:
            identity_ids.extend(identity_ids_of(match))
            private_ids.extend(private_conversation_ids_of(match))
        identity_ids = list(dict.fromkeys(identity_ids))
        private_ids = list(dict.fromkeys(private_ids))

        anchors = mention_anchors(matches[0])
        sender_names = subject_sender_names(matches[0])
        # What he sent, plus the private chats he is part of. Both are exported
        # in full pages: the old "newest ten resources" window is why a value
        # that sits three hundred messages back was invisible.
        main = export_person_scope(
            self.rag,
            owner_user_id=owner_user_id,
            organization_id=organization_id,
            request_id=request_id,
            trace_id=trace_id,
            identity_ids=identity_ids,
            private_ids=private_ids,
            subject=subject or name,
            source_scope=(SOURCE_SENT, SOURCE_PRIVATE),
        )
        # Mentions are anchored per platform name: searching the pronoun "我"
        # matches almost every sentence, and one request cannot carry several
        # anchors because ``content_contains`` is an AND.
        mention_items: list[dict[str, Any]] = []
        truncated = main.truncated
        for anchor in anchors:
            part = export_person_scope(
                self.rag,
                owner_user_id=owner_user_id,
                organization_id=organization_id,
                request_id=request_id,
                trace_id=trace_id,
                identity_ids=identity_ids,
                private_ids=private_ids,
                subject=anchor,
                source_scope=(SOURCE_MENTION,),
            )
            mention_items.extend(part.chunks)
            truncated = truncated or part.truncated

        chunks = chunks_from_scope_items([*main.chunks, *mention_items])
        groups: list[tuple[str, tuple[str, ...]]] = []
        for group in (SOURCE_SENT, SOURCE_PRIVATE, SOURCE_MENTION):
            ids = list(
                dict.fromkeys(
                    chunk.resource_id
                    for chunk in chunks
                    if chunk.source_group == group and chunk.resource_id
                )
            )
            if ids:
                groups.append((group, tuple(ids)))
        return PersonScopeResult(
            subject=subject or name,
            groups=tuple(groups),
            chunks=tuple(chunks),
            anchors=anchors,
            subject_names=sender_names,
            truncated=truncated,
        )
