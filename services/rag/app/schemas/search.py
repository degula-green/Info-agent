from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=2000)
    user_id: str | None = None
    organization_id: str | None = None
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=8, ge=1, le=50)
    include_protected: bool = True
    sender_name: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None

    @field_validator("knowledge_base_ids")
    @classmethod
    def clean_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


class AIDocumentBody(SearchBody):
    conversation_id: str | None = None
    mode: str = Field(default="quick", pattern="^(quick|deep)$")


class TreeSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    user_id: str | None = None
    organization_id: str | None = None
    knowledge_base_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    tree_types: list[str] | None = Field(default=None, max_length=2)
    top_k: int = Field(default=8, ge=1, le=50)
    include_protected: bool = False
    occurred_after: str | None = None
    occurred_before: str | None = None
    conversation_id: str | None = None
