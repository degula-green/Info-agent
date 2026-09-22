from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


FACT_TYPES = {"event", "state", "task", "relation"}


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()) or "general"


@dataclass(frozen=True)
class FactCandidate:
    fact_type: str
    text: str
    subject: str
    entity_type: str = "unknown"
    predicate: str = "related_to"
    normalized_value: dict[str, Any] = field(default_factory=dict)
    topic: str = "general"
    phase: str = "general"
    occurred_at: str | None = None
    confidence: float = 1.0
    chunk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.fact_type not in FACT_TYPES:
            raise ValueError(f"unsupported fact type: {self.fact_type}")
        if not self.text.strip() or not self.subject.strip():
            raise ValueError("fact text and subject are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("fact confidence must be between 0 and 1")

    @property
    def dedupe_key(self) -> str:
        payload = {
            "fact_type": self.fact_type,
            "subject": normalize_key(self.subject),
            "predicate": normalize_key(self.predicate),
            "value": self.normalized_value,
            "time": (self.occurred_at or "")[:10],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceRouteCandidate:
    subject: str
    entity_type: str = "organization"
    topic: str = "general"
    phase: str = "general"
    relation_type: str = "reference"
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("source route subject is required")
        if self.relation_type not in {"primary", "evidence", "reference", "policy"}:
            raise ValueError(f"unsupported source relation type: {self.relation_type}")
        if not 0 <= self.confidence <= 1:
            raise ValueError("source route confidence must be between 0 and 1")


@dataclass(frozen=True)
class MemoryGraph:
    source_id: str
    knowledge_item_id: str
    nodes: tuple[dict[str, Any], ...]
    fact_projections: tuple[dict[str, Any], ...]
    source_projections: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TreeSearchRequest:
    query: str
    user_id: str
    organization_id: str | None
    knowledge_base_id: str | None = None
    knowledge_base_ids: tuple[str, ...] = ()
    tree_types: tuple[str, ...] | None = None
    top_k: int = 8
    include_protected: bool = False
    authorized_object_keys: tuple[str, ...] = ()
    occurred_after: str | None = None
    occurred_before: str | None = None
    conversation_id: str | None = None
    expand_context: bool = True
    context_window_minutes: int = 10
    context_limit: int = 6
