from __future__ import annotations

from pydantic import BaseModel, Field


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=2000)
    user_id: str | None = None
    organization_id: str | None = None
    knowledge_base_id: str | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    include_protected: bool = True
    sender_name: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None


class AIDocumentBody(SearchBody):
    conversation_id: str | None = None
