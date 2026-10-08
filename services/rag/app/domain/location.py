"""Types for the five-layer entity location pipeline.

Mirrors 《实体定位五层管线接口草案》. The distinction that matters throughout:
a *mention* is a span of the query that may name something, and a
*LocatedEntity* is the decision that it refers to a specific registry entity.
One query can carry several mentions, so nothing may return early on the first
match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EntityMention:
    mention_id: str
    surface_form: str
    normalized_form: str
    start: int | None = None
    end: int | None = None
    type_hint: str | None = None
    is_deictic: bool = False
    source: str = "query"


@dataclass(frozen=True)
class EntityCandidate:
    entity_id: str
    domain: str
    canonical_name: str
    match_method: str
    match_score: float
    registry_version: int = 1
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocatedEntity:
    mention_id: str
    entity_id: str
    domain: str
    canonical_name: str
    match_method: str
    match_score: float
    verified: bool = False


@dataclass(frozen=True)
class EntityScope:
    entity_ids: tuple[str, ...]
    composition: str
    min_mount_confidence: float
    residual_query: str
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocateRequest:
    scope_type: str
    scope_id: str
    query: str
    conversation_id: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    context_messages: tuple[str, ...] = ()
    top_k: int = 10
    min_confidence: float = 0.7
    min_mount_confidence: float | None = None
    allow_llm: bool = True


@dataclass
class LocateResult:
    entities: tuple[LocatedEntity, ...]
    scope: EntityScope
    diagnostics: dict[str, Any] = field(default_factory=dict)
