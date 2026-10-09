from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.config import settings
from app.domain.rag import (
    Chunk,
    Entity,
    EntityAlias,
    EntityMount,
    normalized_text,
)


@dataclass(frozen=True)
class EntityMatch:
    entity_id: str
    domain: str
    canonical_name: str
    alias: str
    match_method: str
    match_score: float


class EntityMatcher:
    def __init__(
        self,
        entities: Iterable[Entity],
        aliases: Iterable[EntityAlias],
        *,
        registry_version: int,
    ) -> None:
        self.entities = {entity.id: entity for entity in entities if entity.status == "active"}
        entries: list[tuple[str, str, str, str]] = []
        for entity in self.entities.values():
            values = {entity.canonical_name, entity.normalized_key}
            for value in values:
                normalized = normalized_text(value)
                if normalized:
                    entries.append((normalized, entity.id, entity.domain, "exact"))
        for alias in aliases:
            if alias.status != "active" or alias.entity_id not in self.entities:
                continue
            normalized = normalized_text(alias.normalized_alias or alias.display_alias)
            if normalized:
                entries.append((normalized, alias.entity_id, alias.domain, "alias"))
        # Longest-first is deterministic and avoids a generic short name hiding
        # a more precise registry entry.
        self.entries = sorted(set(entries), key=lambda item: (-len(item[0]), item[1]))
        self.registry_version = max(1, int(registry_version or 1))

    def match_text(self, value: str) -> list[EntityMatch]:
        haystack = normalized_text(value)
        if not haystack:
            return []
        matches: dict[str, EntityMatch] = {}
        consumed: list[tuple[int, int]] = []
        for needle, entity_id, domain, method in self.entries:
            start = haystack.find(needle)
            if start < 0:
                continue
            end = start + len(needle)
            if any(start < used_end and end > used_start for used_start, used_end in consumed):
                continue
            entity = self.entities[entity_id]
            matches[entity_id] = EntityMatch(
                entity_id=entity_id,
                domain=domain,
                canonical_name=entity.canonical_name,
                alias=needle,
                match_method=method,
                match_score=1.0 if method == "exact" else 0.95,
            )
            consumed.append((start, end))
            if len(matches) >= settings.tree_max_entities:
                break
        return list(matches.values())


def match_chunk_mounts(
    chunk: Chunk,
    matcher: EntityMatcher | None,
) -> list[EntityMount]:
    """Resolve which entities a chunk explicitly mentions.

    Time is deliberately not encoded into the mount any more: the search path
    filters on occurred_after/occurred_before metadata instead of a monthly
    branch key.
    """
    if matcher is None:
        return []
    haystack = "\n".join(
        value for value in (
            chunk.title,
            chunk.file_name,
            " / ".join(chunk.heading_path),
            chunk.content,
        ) if value
    )
    output: list[EntityMount] = []
    for match in matcher.match_text(haystack):
        output.append(EntityMount(
            entity_id=match.entity_id,
            domain=match.domain,
            registry_version=matcher.registry_version,
            mount_method="explicit",
            confidence=match.match_score,
        ))
    return output[: settings.tree_max_mounts_per_chunk]


_CANDIDATE_PATTERNS = (
    re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,40}(?:公司|集团|学院|大学|部门|中心|委员会|办公室)"),
    re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,40}(?:项目|平台|系统|网站|官网|应用)"),
    re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,40}(?:制度|办法|条例|规定|政策)"),
    re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,40}(?:合同|协议|订单)"),
)


def discover_candidate_names(value: str) -> list[tuple[str, str]]:
    text = str(value or "")
    output: dict[str, str] = {}
    for index, pattern in enumerate(_CANDIDATE_PATTERNS):
        for match in pattern.findall(text):
            name = match.strip()
            if 2 <= len(name) <= 80:
                output.setdefault(name, _domain_for_pattern(index))
    return list(output.items())


def _domain_for_pattern(index: int) -> str:
    return ("organization", "project", "policy", "contract")[index]
