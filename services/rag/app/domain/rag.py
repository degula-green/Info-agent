from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


DOMAINS = {
    "organization",
    "project",
    "person",
    "policy",
    "contract",
    "asset",
    "location",
    "unclassified",
}
RESOURCE_TYPES = {"message", "attachment"}
CONTENT_VARIANTS = {"display", "protected"}
TREE_MODES = {"off", "shadow", "boost"}
JOB_STATUSES = {
    "pending",
    "processing",
    "ready",
    "metadata_only",
    "failed",
    "cancelled",
}
RAG_STATUSES = {"pending", "processing", "ready", "metadata_only", "failed", "cancelled"}


def compact_strings(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value and value.strip())


def normalized_text(value: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"[\s\-_/\\·•,，。.;；:：'\"“”‘’()（）\[\]【】]+", "", text)
    return text


def time_bucket(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m")
    except (TypeError, ValueError):
        return None


def stable_id(*parts: Any, length: int = 64) -> str:
    value = hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()
    return value[:length]


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class AuthorizationScope:
    scope_type: str
    scope_id: str
    snapshot_id: str | None = None
    expires_at: str | None = None
    authorized_organization_ids: tuple[str, ...] = ()
    authorized_conversation_group_ids: tuple[str, ...] = ()
    authorized_protected_object_keys: tuple[str, ...] = ()
    available: bool = True
    truncated: bool = False

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"


@dataclass(frozen=True)
class AccessCheck:
    resource_type: str
    resource_part: str
    resource_id: str
    action: str = "view"


@dataclass(frozen=True)
class ResourceContext:
    knowledge_item_id: str
    resource_type: str
    resource_id: str
    knowledge_base_id: str
    scope_type: str
    scope_id: str
    content_version: int
    content_hash: str
    acl_version: int = 0
    source_conversation_id: str | None = None
    source_conversation_type: str | None = None
    source_audience_policy: str | None = None
    external_conversation_id: str | None = None
    source_message_id: str | None = None
    source_attachment_id: str | None = None
    sender_identity_id: str | None = None
    sender_platform: str | None = None
    sender_workspace_key: str = ""
    sender_external_user_id: str | None = None
    sender_mapped_user_id: str | None = None
    sender_display_name: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int = 0
    object_ref: str | None = None
    content_type: str | None = None
    access_scope: str | None = None
    sensitivity: str | None = None
    lifecycle_status: str = "active"
    title: str | None = None
    sent_at: str | None = None
    content_access_required: bool = False
    has_display_content: bool = False
    has_protected_content: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"

    @property
    def resource_ref(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    @classmethod
    def from_event_and_source(cls, event_payload: dict[str, Any], source: dict[str, Any]) -> "ResourceContext":
        merged = {**event_payload, **source}
        scope_type = str(merged.get("scope_type") or "")
        scope_id = str(merged.get("scope_id") or "")
        if not scope_type or not scope_id:
            organization_id = str(merged.get("organization_id") or "").strip()
            owner_user_id = str(merged.get("owner_user_id") or "").strip()
            if organization_id:
                scope_type, scope_id = "organization", organization_id
            elif owner_user_id:
                scope_type, scope_id = "user", owner_user_id
        if scope_type not in {"organization", "user"} or not scope_id:
            raise ValueError("source did not resolve a valid scope")
        resource_type = str(merged.get("resource_type") or "")
        if resource_type not in RESOURCE_TYPES:
            raise ValueError("resource_type must be message or attachment")
        knowledge_item_id = str(merged.get("knowledge_item_id") or "")
        resource_id = str(merged.get("resource_id") or "")
        knowledge_base_id = str(merged.get("knowledge_base_id") or "")
        if not all((knowledge_item_id, resource_id, knowledge_base_id)):
            raise ValueError("source is missing knowledge_item_id, resource_id, or knowledge_base_id")
        content_hash = str(merged.get("content_hash") or "").removeprefix("sha256:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("source did not resolve a valid content_hash")
        display_required = bool(merged.get("content_access_required", False))
        return cls(
            knowledge_item_id=knowledge_item_id,
            resource_type=resource_type,
            resource_id=resource_id,
            knowledge_base_id=knowledge_base_id,
            scope_type=scope_type,
            scope_id=scope_id,
            content_version=int(merged.get("content_version") or 1),
            content_hash=content_hash,
            acl_version=int(merged.get("acl_version") or 0),
            source_conversation_id=_text_or_none(merged.get("source_conversation_id")),
            source_conversation_type=_text_or_none(merged.get("source_conversation_type")),
            source_audience_policy=_text_or_none(merged.get("source_audience_policy")),
            external_conversation_id=_text_or_none(merged.get("external_conversation_id")),
            source_message_id=_text_or_none(merged.get("source_message_id")),
            source_attachment_id=_text_or_none(merged.get("source_attachment_id")),
            sender_identity_id=_text_or_none(merged.get("sender_identity_id")),
            sender_platform=_text_or_none(merged.get("sender_platform")),
            sender_workspace_key=str(merged.get("sender_workspace_key") or ""),
            sender_external_user_id=_text_or_none(merged.get("sender_external_user_id")),
            sender_mapped_user_id=_text_or_none(merged.get("sender_mapped_user_id")),
            sender_display_name=_text_or_none(merged.get("sender_display_name")),
            file_name=_text_or_none(merged.get("file_name")),
            mime_type=_text_or_none(merged.get("mime_type")),
            size_bytes=int(merged.get("size_bytes") or 0),
            object_ref=_text_or_none(merged.get("object_ref")),
            content_type=_text_or_none(merged.get("content_type")),
            access_scope=_text_or_none(merged.get("access_scope")),
            sensitivity=_text_or_none(merged.get("sensitivity")),
            lifecycle_status=str(merged.get("lifecycle_status") or "active"),
            title=_text_or_none(merged.get("title")),
            sent_at=_text_or_none(merged.get("sent_at") or merged.get("collected_at")),
            content_access_required=display_required,
            has_display_content=True,
            has_protected_content=display_required,
            raw=merged,
        )

    @property
    def auth_partition_key(self) -> str:
        if self.source_audience_policy == "source_conversation_members" and self.source_conversation_id:
            return f"conversation:{self.source_conversation_id}"
        if self.scope_type == "organization":
            return f"org:{self.scope_id}"
        return f"user:{self.scope_id}"

    def auth_object_key(self, variant: str) -> str | None:
        if variant != "protected":
            return None
        if self.resource_type == "attachment":
            return f"attachment_content:{self.resource_id}"
        return f"knowledge_original:{self.knowledge_item_id}"


@dataclass
class Chunk:
    chunk_id: str
    resource_snapshot_id: str
    knowledge_item_id: str
    resource_type: str
    resource_id: str
    knowledge_base_id: str
    scope_type: str
    scope_id: str
    content_version: int
    processing_version: str
    chunking_version: str
    content_variant: str
    chunk_index: int
    chunk_count: int
    content: str
    content_hash: str
    title: str | None = None
    file_name: str | None = None
    heading_path: tuple[str, ...] = ()
    context_header: dict[str, Any] = field(default_factory=dict)
    source_locator: dict[str, Any] = field(default_factory=dict)
    source_conversation_id: str | None = None
    conversation_type: str | None = None
    document_id: str | None = None
    message_id: str | None = None
    sent_at: str | None = None
    auth_partition_key: str | None = None
    auth_object_key: str | None = None
    acl_version: int = 0
    sensitivity: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    embedding_status: str = "pending"
    rag_eligible: bool = True
    lifecycle_status: str = "active"
    embedding: list[float] | None = None
    branch_keys: tuple[str, ...] = ()
    registry_version: int = 0
    source_kind: str | None = None

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"

    @property
    def protected(self) -> bool:
        return self.content_variant == "protected"

    @property
    def logical_position_key(self) -> str:
        return stable_id(
            self.knowledge_item_id,
            self.content_version,
            self.processing_version,
            self.chunking_version,
            self.chunk_index,
        )

    @classmethod
    def create(
        cls,
        *,
        context: ResourceContext,
        snapshot_id: str,
        chunk_index: int,
        chunk_count: int,
        content: str,
        variant: str,
        processing_version: str,
        chunking_version: str,
        title: str | None = None,
        file_name: str | None = None,
        heading_path: tuple[str, ...] = (),
        source_locator: dict[str, Any] | None = None,
    ) -> "Chunk":
        if variant not in CONTENT_VARIANTS:
            raise ValueError("content_variant must be display or protected")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunk_id = stable_id(
            context.knowledge_item_id,
            context.resource_type,
            context.resource_id,
            context.content_version,
            processing_version,
            chunking_version,
            variant,
            chunk_index,
        )
        return cls(
            chunk_id=chunk_id,
            resource_snapshot_id=snapshot_id,
            knowledge_item_id=context.knowledge_item_id,
            resource_type=context.resource_type,
            resource_id=context.resource_id,
            knowledge_base_id=context.knowledge_base_id,
            scope_type=context.scope_type,
            scope_id=context.scope_id,
            content_version=context.content_version,
            processing_version=processing_version,
            chunking_version=chunking_version,
            content_variant=variant,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            content=content,
            content_hash=content_hash,
            title=title,
            file_name=file_name,
            heading_path=heading_path,
            context_header={
                "title": title,
                "file_name": file_name,
                "heading_path": list(heading_path),
                "source_kind": context.resource_type,
                "source_audience_policy": context.source_audience_policy,
                "external_conversation_id": context.external_conversation_id,
                "sender_identity_id": context.sender_identity_id,
                "sender_platform": context.sender_platform,
                "sender_display_name": context.sender_display_name,
            },
            source_locator=dict(source_locator or {}),
            source_conversation_id=context.source_conversation_id,
            conversation_type=context.source_conversation_type,
            document_id=context.resource_id if context.resource_type == "attachment" else None,
            message_id=context.source_message_id if context.resource_type == "attachment" else context.resource_id,
            sent_at=context.sent_at,
            auth_partition_key=context.auth_partition_key,
            auth_object_key=context.auth_object_key(variant),
            acl_version=context.acl_version,
            sensitivity=context.sensitivity,
            rag_eligible=bool(content.strip()),
            source_kind=context.resource_type,
        )

    def es_source(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "logical_position_key": self.logical_position_key,
            "resource_snapshot_id": self.resource_snapshot_id,
            "knowledge_item_id": self.knowledge_item_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "knowledge_base_id": self.knowledge_base_id,
            "document_id": self.document_id,
            "message_id": self.message_id,
            "source_conversation_id": self.source_conversation_id,
            "external_conversation_id": self.context_header.get("external_conversation_id"),
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "scope_key": self.scope_key,
            "source_conversation_type": self.conversation_type,
            "source_audience_policy": self.context_header.get("source_audience_policy"),
            "auth_partition_key": self.auth_partition_key,
            "auth_object_key": self.auth_object_key,
            "acl_version": self.acl_version,
            "sensitivity": self.sensitivity,
            "rag_eligible": self.rag_eligible,
            "lifecycle_status": self.lifecycle_status,
            "content": self.content,
            "content_hash": self.content_hash,
            "content_version": self.content_version,
            "processing_version": self.processing_version,
            "chunking_version": self.chunking_version,
            "content_variant": self.content_variant,
            "chunk_index": self.chunk_index,
            "chunk_count": self.chunk_count,
            "title": self.title,
            "file_name": self.file_name,
            "heading_path": " / ".join(self.heading_path) if self.heading_path else None,
            "sender_identity_id": self.context_header.get("sender_identity_id"),
            "sender_platform": self.context_header.get("sender_platform"),
            "sender_display_name": self.context_header.get("sender_display_name"),
            "sent_at": self.sent_at,
            "context_header": self.context_header,
            "source_locator": self.source_locator,
            "branch_keys": list(self.branch_keys),
            "registry_version": self.registry_version,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
        }
        if self.embedding is not None and self.rag_eligible:
            value["embedding"] = self.embedding
        return {key: item for key, item in value.items() if item is not None}


@dataclass(frozen=True)
class SearchRequest:
    query: str
    user_id: str
    scope_type: str
    scope_id: str
    knowledge_base_ids: tuple[str, ...] = ()
    entry: str = "global"
    top_k: int = 8
    include_protected: bool = False
    occurred_after: str | None = None
    occurred_before: str | None = None
    qa_mode: str = "quick"
    conversation_id: str | None = None
    branch_keys: tuple[str, ...] = ()

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"


@dataclass
class SearchResult:
    chunk_id: str
    content: str
    score: float = 0.0
    rank: int = 0
    source: dict[str, Any] = field(default_factory=dict)
    highlight: str | None = None

    @property
    def knowledge_item_id(self) -> str | None:
        value = self.source.get("knowledge_item_id")
        return str(value) if value else None

    @property
    def resource_id(self) -> str | None:
        value = self.source.get("resource_id")
        return str(value) if value else None

    @property
    def branch_keys(self) -> tuple[str, ...]:
        values = self.source.get("branch_keys") or ()
        return tuple(str(value) for value in values if value)

    def safe_dict(self) -> dict[str, Any]:
        source = {
            key: value
            for key, value in self.source.items()
            if key not in {"embedding", "auth_object_key"}
        }
        return {
            "chunk_id": self.chunk_id,
            "content": self.content if bool(source.get("rag_eligible", True)) else "",
            "score": self.score,
            "rank": self.rank,
            "highlight": self.highlight,
            "source": source,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.safe_dict()


@dataclass(frozen=True)
class Entity:
    id: str
    scope_type: str
    scope_id: str
    domain: str
    canonical_name: str
    normalized_key: str
    status: str = "active"
    registry_version: int = 1


@dataclass(frozen=True)
class EntityAlias:
    id: str
    entity_id: str
    scope_type: str
    scope_id: str
    domain: str
    display_alias: str
    normalized_alias: str
    status: str = "active"


@dataclass(frozen=True)
class BranchMatch:
    branch_key: str
    entity_id: str
    domain: str
    registry_version: int
    match_method: str = "exact"
    match_score: float = 1.0


@dataclass
class Candidate:
    id: str
    scope_type: str
    scope_id: str
    candidate_name: str
    normalized_key: str
    candidate_domain: str
    mention_count: int = 0
    distinct_chunk_count: int = 0
    distinct_source_count: int = 0
    distinct_conversation_count: int = 0
    sample_context: str | None = None
    score: float = 0.0
    status: str = "new"


def _text_or_none(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
