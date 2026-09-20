from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class AttachmentContext:
    """The minimal, versioned input required by the processing pipeline."""

    attachment_id: str
    file_name: str
    mime_type: str
    size_bytes: int = 0
    object_ref: str | None = None
    file_path: str | None = None
    source_content_hash: str | None = None
    content_version: int = 1
    attachment_content_version: int | None = None
    acl_version: int = 0
    content_access_required: bool = False
    organization_id: str | None = None
    knowledge_item_id: str | None = None
    knowledge_base_id: str | None = None
    knowledge_scope: str | None = None
    access_scope: str | None = None
    title: str | None = None
    owner_user_id: str | None = None
    conversation_group_id: str | None = None
    conversation_ingestion_id: str | None = None
    external_conversation_id: str | None = None
    message_id: str | None = None
    external_message_id: str | None = None
    sender_identity_id: str | None = None
    sender_display_name: str | None = None
    sent_at: str | None = None
    sensitivity: str | None = None
    lifecycle_status: str = "active"
    part_kind: str = "attachment_content"
    source_locator: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AttachmentContext":
        attachments = value.get("attachments")
        if attachments and isinstance(attachments, list):
            # A KnowledgeItem payload can be adapted without changing the
            # downstream parser contract. The first attachment is handled by
            # one processing job; callers can create one job per attachment.
            merged = {**value, **(attachments[0] or {})}
        else:
            merged = value
        source = merged.get("source") if isinstance(merged.get("source"), dict) else {}
        attachment_id = str(merged.get("attachment_id") or merged.get("id") or "")
        if not attachment_id:
            raise ValueError("attachment_id is required")
        file_name = str(merged.get("file_name") or merged.get("name") or "")
        if not file_name:
            raise ValueError("file_name is required")
        return cls(
            attachment_id=attachment_id,
            file_name=file_name,
            mime_type=str(merged.get("mime_type") or "application/octet-stream"),
            size_bytes=int(merged.get("size_bytes") or 0),
            object_ref=merged.get("object_ref") or merged.get("storage_key"),
            file_path=merged.get("file_path"),
            source_content_hash=merged.get("source_content_hash") or merged.get("content_hash"),
            content_version=int(merged.get("content_version") or 1),
            attachment_content_version=_optional_int(merged.get("attachment_content_version")),
            acl_version=int(merged.get("acl_version") or merged.get("auth_acl_version") or 0),
            content_access_required=_as_bool(merged.get("content_access_required", False)),
            organization_id=_optional_text(merged.get("organization_id")),
            knowledge_item_id=_optional_text(merged.get("knowledge_item_id")),
            knowledge_base_id=_optional_text(merged.get("knowledge_base_id")),
            knowledge_scope=_optional_text(merged.get("knowledge_scope")),
            access_scope=_optional_text(merged.get("access_scope")),
            title=_optional_text(merged.get("title") or merged.get("document_title")),
            owner_user_id=_optional_text(merged.get("owner_user_id")),
            conversation_group_id=_optional_text(merged.get("conversation_group_id") or source.get("conversation_id")),
            conversation_ingestion_id=_optional_text(merged.get("conversation_ingestion_id")),
            external_conversation_id=_optional_text(merged.get("external_conversation_id") or source.get("conversation_id")),
            message_id=_optional_text(merged.get("message_id")),
            external_message_id=_optional_text(merged.get("external_message_id") or source.get("external_message_id")),
            sender_identity_id=_optional_text(merged.get("sender_identity_id") or source.get("sender_identity_id")),
            sender_display_name=_optional_text(merged.get("sender_display_name")),
            sent_at=_optional_text(merged.get("sent_at")),
            sensitivity=_optional_text(merged.get("sensitivity")),
            lifecycle_status=str(merged.get("lifecycle_status") or "active"),
            part_kind=str(merged.get("part_kind") or "attachment_content"),
            source_locator=dict(merged.get("source_locator") or {}),
        )


def _optional_text(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected an integer value") from exc


@dataclass(frozen=True)
class CanonicalBlock:
    page_number: int | None
    order: int
    type: str
    text: str = ""
    html: str | None = None
    latex: str | None = None
    asset_ref: str | None = None
    bbox: tuple[float, ...] | None = None
    heading_path: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        values = [" / ".join(self.heading_path), self.text, self.html or "", self.latex or ""]
        return "\n".join(value for value in values if value).strip()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "page_number": self.page_number,
            "order": self.order,
            "type": self.type,
            "text": self.text,
            "heading_path": list(self.heading_path),
        }
        if self.html is not None:
            result["html"] = self.html
        if self.latex is not None:
            result["latex"] = self.latex
        if self.asset_ref is not None:
            result["asset_ref"] = self.asset_ref
        if self.bbox is not None:
            result["bbox"] = list(self.bbox)
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class ParsedDocument:
    markdown: str
    blocks: list[CanonicalBlock]
    parser: str
    parser_version: str
    artifact_ref: str | None = None
    canonical_ref: str | None = None
    asset_refs: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    # Optional MinerU diagnostics kept as relative paths inside the result
    # bundle (for example ``middle.json`` and ``model.json``).
    auxiliary_files: dict[str, str] = field(default_factory=dict)


@dataclass
class ChunkRecord:
    chunk_id: str
    chunk_index: int
    chunk_count: int
    chunking_version: str
    knowledge_item_id: str
    attachment_id: str | None
    knowledge_base_id: str | None
    content_version: int
    attachment_content_version: int | None
    auth_acl_version: int
    mapping_version: str
    auth_resource_type: str
    auth_resource_part: str
    auth_resource_id: str
    auth_object_key: str
    knowledge_scope: str | None
    access_scope: str | None
    organization_id: str | None
    owner_user_id: str | None
    conversation_group_id: str | None
    part_kind: str
    content_access_required: bool
    title: str | None
    content: str
    content_hash: str
    content_visibility: str
    embedding_model: str | None = None
    embedding: list[float] | None = None
    vectorized: bool = False
    rag_eligible: bool = True
    conversation_ingestion_id: str | None = None
    external_conversation_id: str | None = None
    message_id: str | None = None
    external_message_id: str | None = None
    sender_identity_id: str | None = None
    sender_display_name: str | None = None
    sent_at: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    source_locator: dict[str, Any] = field(default_factory=dict)
    sensitivity: str | None = None
    lifecycle_status: str = "active"
    created_at: str | None = None
    indexed_at: str | None = None
    chunk_type: str = "text"

    @property
    def protected(self) -> bool:
        return self.auth_resource_part in {"original", "content"} and (
            self.content_access_required or self.part_kind == "knowledge_original"
        )

    def as_es_source(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "chunk_count": self.chunk_count,
            "chunking_version": self.chunking_version,
            "knowledge_item_id": self.knowledge_item_id,
            "attachment_id": self.attachment_id,
            "knowledge_base_id": self.knowledge_base_id,
            "content_version": self.content_version,
            "attachment_content_version": self.attachment_content_version,
            "auth_acl_version": self.auth_acl_version,
            "mapping_version": self.mapping_version,
            "auth_resource_type": self.auth_resource_type,
            "auth_resource_part": self.auth_resource_part,
            "auth_resource_id": self.auth_resource_id,
            "auth_object_key": self.auth_object_key,
            "knowledge_scope": self.knowledge_scope,
            "access_scope": self.access_scope,
            "organization_id": self.organization_id,
            "owner_user_id": self.owner_user_id,
            "conversation_group_id": self.conversation_group_id,
            "part_kind": self.part_kind,
            "chunk_type": self.chunk_type,
            "content_access_required": self.content_access_required,
            "title": self.title,
            "content": self.content,
            "content_hash": self.content_hash,
            "content_visibility": self.content_visibility,
            "embedding_model": self.embedding_model,
            "vectorized": self.vectorized,
            "rag_eligible": self.rag_eligible,
            "conversation_ingestion_id": self.conversation_ingestion_id,
            "external_conversation_id": self.external_conversation_id,
            "message_id": self.message_id,
            "external_message_id": self.external_message_id,
            "sender_identity_id": self.sender_identity_id,
            "sender_display_name": self.sender_display_name,
            "sent_at": self.sent_at,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "source_locator": self.source_locator,
            "sensitivity": self.sensitivity,
            "lifecycle_status": self.lifecycle_status,
            "created_at": self.created_at,
            "indexed_at": self.indexed_at,
        }
        if self.embedding is not None and self.rag_eligible:
            value["embedding"] = self.embedding
        return {key: item for key, item in value.items() if item is not None}


@dataclass(frozen=True)
class SearchRequest:
    query: str
    user_id: str
    organization_id: str | None = None
    knowledge_base_id: str | None = None
    knowledge_base_ids: tuple[str, ...] = ()
    entry: str = "global"
    resource_types: tuple[str, ...] = ()
    sender_name: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    top_k: int = 8
    include_protected: bool = True
    conversation_id: str | None = None
    qa_mode: str | None = None
    source_attachment_ids: tuple[str, ...] = ()
    source_knowledge_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationScope:
    snapshot_id: str | None = None
    expires_at: str | None = None
    objects: dict[str, tuple[str, ...]] = field(default_factory=dict)
    available: bool = True

    @property
    def object_keys(self) -> tuple[str, ...]:
        return tuple(key for values in self.objects.values() for key in values)


@dataclass
class SearchResult:
    chunk_id: str
    content: str
    score: float = 0.0
    rank: int = 0
    rerank_score: float | None = None
    source: dict[str, Any] = field(default_factory=dict)
    highlight: str | None = None

    @property
    def knowledge_item_id(self) -> str | None:
        value = self.source.get("knowledge_item_id")
        return str(value) if value is not None else None

    @property
    def attachment_id(self) -> str | None:
        value = self.source.get("attachment_id")
        return str(value) if value is not None else None

    @property
    def auth_resource_type(self) -> str:
        return str(self.source.get("auth_resource_type") or "knowledge_item")

    @property
    def auth_resource_part(self) -> str:
        return str(self.source.get("auth_resource_part") or "display")

    @property
    def auth_resource_id(self) -> str:
        return str(self.source.get("auth_resource_id") or self.knowledge_item_id or self.chunk_id)

    def as_dict(self) -> dict[str, Any]:
        # Metadata-only chunks may participate in BM25 file-name searches,
        # but their body must never be returned to a caller or QA prompt.
        visible_content = self.content if bool(self.source.get("rag_eligible", True)) else ""
        safe_source = {key: value for key, value in self.source.items() if key not in {"embedding", "auth_object_key"}}
        value = {
            "chunk_id": self.chunk_id,
            "content": visible_content,
            "score": self.score,
            "rank": self.rank,
            "rerank_score": self.rerank_score,
            "highlight": self.highlight,
            **safe_source,
        }
        value["content"] = visible_content
        return value


@dataclass(frozen=True)
class AccessCheck:
    resource_type: str
    resource_part: str
    resource_id: str
    action: str = "view"


def compact_strings(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value and value.strip())
