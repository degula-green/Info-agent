from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=2000)
    scope_type: str = Field(default="organization", pattern="^(organization|user)$")
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=8, ge=1, le=50)
    include_protected: bool = True
    sender_name: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    conversation_id: str | None = None
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
