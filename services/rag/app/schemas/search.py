from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=2000)
    offset: int = Field(default=0, ge=0, le=100000)
    scope_type: str = Field(default="organization", pattern="^(organization|user)$")
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    # The scope export pages through one person's whole window; every page
    # costs an authorization lookup, so a small page size multiplies the
    # slowest part of the request.
    top_k: int = Field(default=8, ge=1, le=200)
    include_protected: bool = True
    sender_name: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    conversation_id: str | None = None
    source_conversation_id: str | None = None
    sender_ids: list[str] = Field(default_factory=list, max_length=20)
    sender_names: list[str] = Field(default_factory=list, max_length=20)
    conversation_ids: list[str] = Field(default_factory=list, max_length=20)
    conversation_names: list[str] = Field(default_factory=list, max_length=20)
    resource_ids: list[str] = Field(default_factory=list, max_length=100)
    content_contains: list[str] = Field(default_factory=list, max_length=10)
    resource_types: list[str] = Field(default_factory=list, max_length=5)
    file_extensions: list[str] = Field(default_factory=list, max_length=20)
    message_types: list[str] = Field(default_factory=list, max_length=10)
    group_by_source: bool = False
    # Controller-only legacy fields. They are normalized before domain/storage.
    organization_id: str | None = None
    user_id: str | None = None

    @field_validator("knowledge_base_ids")
    @classmethod
    def clean_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    def clean_library_ids(self) -> tuple[str, ...]:
        return tuple(self.knowledge_base_ids) or (
            (self.knowledge_base_id,) if self.knowledge_base_id else ()
        )


class SourceSearchBody(SearchBody):
    pass


class ContentSearchBody(SearchBody):
    query: str = Field(min_length=1, max_length=2000)
    group_by_source: bool = True


class KeywordSearchBody(SearchBody):
    """Fast lexical-only search; empty query lists the newest matches."""

    pass


class ScopeSearchBody(SearchBody):
    """Full-scope export: every chunk in a filter-defined person scope."""

    pass


class ContextAnchor(BaseModel):
    """One anchor message the caller wants the conversation around."""

    resource_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    sent_at: str | None = Field(default=None, max_length=64)


class ContextSearchBody(SearchBody):
    """Neighbouring messages in the same conversation, around each anchor.

    A value often sits next to the sentence that names the field ("学号
    20251714203" then a bare "小呆呆"), so the caller asks for the messages
    either side of the ones it already found.
    """

    anchors: list[ContextAnchor] = Field(default_factory=list, max_length=50)
    radius: int = Field(default=2, ge=1, le=10)


class AIDocumentBody(SearchBody):
    conversation_id: str | None = None
    mode: str = Field(default="quick", pattern="^(quick|deep)$")


class TreeSearchBody(SearchBody):
    pass


class QATitleBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class QAConversationBody(BaseModel):
    title: str = Field(default="新的对话", min_length=1, max_length=300)
    retrieval_mode: str = Field(default="quick", pattern="^(quick|deep)$")
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    scope_type: str = Field(default="organization", pattern="^(organization|user)$")
    organization_id: str | None = None

    @field_validator("knowledge_base_ids")
    @classmethod
    def clean_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))
