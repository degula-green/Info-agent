"""Five-layer entity location: L0 mention extraction through L5 summary.

Design constraints that shape this module:

* Location is per **mention**, not per query. "张三在A项目的服务器" carries two
  entity mentions and both have to survive, so no layer may return early for
  the whole query.
* Only the scope is a hard filter. Type, recency and keyword signals are
  ranking weights; using them as filters silently drops the right answer when
  the classifier or the user's phrasing is off.
* Confidence values are per-layer and not comparable across layers, so each
  candidate carries its `match_method` and downstream code gates on the pair.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from app.application.entity_service import discover_candidate_names
from app.config import settings
from app.domain.location import (
    EntityCandidate,
    EntityMention,
    EntityScope,
    LocateRequest,
    LocateResult,
    LocatedEntity,
)
from app.domain.rag import Entity, EntityAlias, normalized_text


# A mention that points at something the conversation established earlier.
# Deliberately phrase-level: a bare "这个" is usually not an entity reference.
_DEICTIC_PHRASES = (
    "这个项目", "那个项目", "这家公司", "那家公司", "这个公司", "那个公司",
    "这个客户", "那个客户", "这份合同", "那份合同", "这个政策", "那个政策",
    "上次说的", "刚才提到的", "前面说的", "上一条",
)

# Score below which the semantic layer is considered unsure and L4 is asked.
_LLM_GATE_SCORE = 0.85
# When L4 cannot answer (timeout, refusal, low confidence) the draft keeps the
# lexical answer only if it was already this strong; otherwise the mention stays
# unresolved rather than being guessed.
_LLM_FALLBACK_SCORE = 0.85
# Top-1/top-2 gap below which the semantic layer cannot separate candidates.
_LLM_GATE_GAP = 0.08


def _ms(started: float) -> float:
    """Elapsed milliseconds, rounded to microsecond precision."""
    return round((time.perf_counter() - started) * 1000, 3)


def _layer_totals(
    traces: Sequence[dict[str, Any]], stage_ms: dict[str, float]
) -> dict[str, float]:
    """Sum per-mention layer times into one figure per layer.

    Location runs once per mention, so a two-mention query performs two L1
    lookups. The budget is per query, so layers are summed, not averaged.
    """
    totals = {layer: round(float(value), 3) for layer, value in stage_ms.items()}
    for trace in traces:
        for entry in trace.get("layer_trace") or []:
            layer = str(entry.get("layer") or "")
            if not layer:
                continue
            totals[layer] = round(
                totals.get(layer, 0.0) + float(entry.get("elapsed_ms") or 0.0), 3
            )
    return totals


class MentionVerifierLike:
    """Structural type for the optional L4 verifier."""

    def verify(
        self,
        *,
        mention: EntityMention,
        candidates: Sequence[EntityCandidate],
        context_messages: Sequence[str],
    ) -> dict[str, Any] | None:  # pragma: no cover - protocol
        raise NotImplementedError


class EntityLocator:
    """Resolve query mentions to registry entities."""

    def __init__(
        self,
        *,
        repository: Any,
        embedding: Any | None = None,
        verifier: MentionVerifierLike | None = None,
        max_entities: int | None = None,
    ) -> None:
        self.repository = repository
        self.embedding = embedding
        self.verifier = verifier
        self.max_entities = max_entities or settings.tree_max_entities

    def locate(self, request: LocateRequest) -> LocateResult:
        started = time.perf_counter()
        # L0 covers the registry read and mention extraction together: both are
        # pure in-process work that runs before any candidate lookup, and the
        # plan's 80ms budget is about this whole locating stage.
        l0_started = time.perf_counter()
        entities, aliases, version = self.repository.load_entity_registry(
            scope_type=request.scope_type, scope_id=request.scope_id
        )
        mentions = extract_mentions(request.query, entities, aliases)
        stage_ms: dict[str, float] = {"L0": _ms(l0_started)}
        if not mentions:
            return self._empty_result(
                request, version, mentions, stage_ms=stage_ms, total_ms=_ms(started)
            )

        located: list[LocatedEntity] = []
        unresolved: list[str] = []
        traces: list[dict[str, Any]] = []
        for mention in mentions:
            resolutions, trace = self._resolve_mention(request, mention, entities, aliases)
            traces.append(trace)
            if not resolutions:
                unresolved.append(mention.surface_form)
            else:
                located.extend(resolutions)

        l5_started = time.perf_counter()
        result = self._summarize(
            request, mentions, located, unresolved, traces, version,
            stage_ms=stage_ms, total_ms=_ms(started),
        )
        result.diagnostics["layer_ms"]["L5"] = _ms(l5_started)
        return result

    # ------------------------------------------------------------------ L1-L4

    def _resolve_mention(
        self,
        request: LocateRequest,
        mention: EntityMention,
        entities: Sequence[Entity],
        aliases: Sequence[EntityAlias],
    ) -> tuple[list[LocatedEntity], dict[str, Any]]:
        trace: dict[str, Any] = {
            "mention_id": mention.mention_id,
            "surface_form": mention.surface_form,
            "layer_trace": [],
            "llm_invoked": False,
        }

        # L1: exact canonical name or alias, all matches kept.
        l1_started = time.perf_counter()
        exact = self.repository.locate_entities_exact(
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            normalized=mention.normalized_form,
        )
        trace["layer_trace"].append({
            "layer": "L1", "method": "exact",
            "candidate_count": len(exact), "elapsed_ms": _ms(l1_started),
        })
        candidates = [self._candidate(item) for item in exact]

        # L2: trigram / edit distance / containment. Containment results are
        # carried forward but not trusted on their own.
        if not candidates:
            l2_started = time.perf_counter()
            fuzzy = self.repository.locate_entities_fuzzy(
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                normalized=mention.normalized_form,
            )
            trace["layer_trace"].append({
                "layer": "L2", "method": "fuzzy",
                "candidate_count": len(fuzzy), "elapsed_ms": _ms(l2_started),
            })
            candidates = [self._candidate(item) for item in fuzzy]

        # L3: semantic. Runs when the lexical layers produced nothing *or* only
        # produced weak matches: a containment hit at 0.70 must not short-circuit
        # the layer that exists to resolve abbreviations ("aims").
        if (not candidates or max(item.match_score for item in candidates) < _LLM_GATE_SCORE) and self.embedding is not None:
            l3_started = time.perf_counter()
            semantic = self._semantic_candidates(request, mention)
            trace["layer_trace"].append({
                "layer": "L3", "method": "semantic",
                "candidate_count": len(semantic), "elapsed_ms": _ms(l3_started),
            })
            if semantic:
                merged = {item.entity_id: item for item in candidates}
                for item in semantic:
                    current = merged.get(item.entity_id)
                    if current is None or item.match_score > current.match_score:
                        merged[item.entity_id] = item
                candidates = list(merged.values())

        if not candidates:
            return [], trace

        candidates = self._rank(mention, candidates)

        # A single self-confident exact match needs no verification. Everything
        # else that is ambiguous or weak asks L4, if L4 is available.
        chosen = candidates[0]
        accepted: list[EntityCandidate]
        verified_attempted = False
        llm_confirmed = False
        if self._needs_verification(mention, candidates, exact) and request.allow_llm and self.verifier is not None:
            l4_started = time.perf_counter()
            verified_attempted = True
            verified = self._verify(request, mention, candidates, trace)
            trace["llm_invoked"] = True
            llm_confirmed = verified is not None
            trace["layer_trace"].append({
                "layer": "L4", "method": "llm",
                "candidate_count": len(candidates), "elapsed_ms": _ms(l4_started),
            })
            accepted = [verified] if verified is not None else []
        elif exact:
            # An exact name may legitimately exist in two domains; keep every
            # match and let downstream scoping cover both.
            accepted = candidates
        else:
            accepted = [chosen]

        if verified_attempted and not accepted and candidates[0].match_score >= _LLM_FALLBACK_SCORE:
            # L4 degraded. A lexical match this strong is still worth keeping,
            # but the trace says it was not verified.
            accepted = [chosen]
            trace["llm_degraded"] = True

        # Containment-only matches are explicitly not trustworthy on their own.
        # If nothing verified this mention, drop it rather than guess.
        if (
            accepted
            and accepted[0].match_method == "substring"
            and not trace["llm_invoked"]
        ):
            trace["rejected"] = {"reason": "containment_requires_verification"}
            return [], trace

        accepted = [item for item in accepted if item.match_score >= request.min_confidence]
        if not accepted:
            trace["rejected"] = {"reason": "below_min_confidence", "score": candidates[0].match_score}
            return [], trace

        trace["chosen"] = {
            "entity_id": accepted[0].entity_id,
            "match_method": accepted[0].match_method,
            "match_score": accepted[0].match_score,
        }
        return (
            [
                LocatedEntity(
                    mention_id=mention.mention_id,
                    entity_id=item.entity_id,
                    domain=item.domain,
                    canonical_name=item.canonical_name,
                    match_method=item.match_method,
                    match_score=item.match_score,
                    # "verified" means L4 confirmed this entity, not merely that
                    # L4 was called: a degraded fallback keeps the lexical
                    # answer and must not be labelled as verified.
                    verified=llm_confirmed,
                )
                for item in accepted
            ],
            trace,
        )

    def _semantic_candidates(
        self, request: LocateRequest, mention: EntityMention
    ) -> list[EntityCandidate]:
        try:
            vectors = self.embedding.embed([mention.surface_form])
        except Exception:
            return []
        if not vectors:
            return []
        found = self.repository.locate_entities_semantic(
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            embedding=vectors[0],
            limit=request.top_k,
        )
        return [self._candidate(item) for item in found]

    def _rank(
        self, mention: EntityMention, candidates: list[EntityCandidate]
    ) -> list[EntityCandidate]:
        """Apply soft signals, then order.

        Type and recency only reorder; they never remove a candidate, because a
        wrong type label or an old-but-correct entity must still be reachable.
        """
        adjusted: list[EntityCandidate] = []
        for candidate in candidates:
            score = candidate.match_score
            if mention.type_hint and candidate.domain == mention.type_hint:
                score = min(1.0, score * 1.05)
            adjusted.append(
                EntityCandidate(
                    entity_id=candidate.entity_id,
                    domain=candidate.domain,
                    canonical_name=candidate.canonical_name,
                    match_method=candidate.match_method,
                    match_score=round(score, 4),
                    registry_version=candidate.registry_version,
                    evidence=dict(candidate.evidence),
                )
            )
        adjusted.sort(key=lambda item: (-item.match_score, item.entity_id))
        return adjusted

    def _needs_verification(
        self,
        mention: EntityMention,
        candidates: Sequence[EntityCandidate],
        exact: Sequence[dict[str, Any]],
    ) -> bool:
        if mention.is_deictic:
            return True
        if not exact and candidates:
            top = candidates[0]
            if top.match_method == "substring":
                return True
            if top.match_score < _LLM_GATE_SCORE:
                return True
        if len(candidates) >= 2:
            gap = candidates[0].match_score - candidates[1].match_score
            if gap < _LLM_GATE_GAP:
                return True
        return False

    def _verify(
        self,
        request: LocateRequest,
        mention: EntityMention,
        candidates: Sequence[EntityCandidate],
        trace: dict[str, Any],
    ) -> EntityCandidate | None:
        try:
            decision = self.verifier.verify(
                mention=mention,
                candidates=candidates,
                context_messages=request.context_messages,
            )
        except Exception:
            return None
        if not isinstance(decision, dict):
            return None
        entity_id = str(decision.get("entity_id") or "")
        trace["llm_decision"] = {
            "entity_id": entity_id,
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
        }
        # The model may only choose from the offered set; anything else is a
        # hallucination and gets discarded rather than trusted.
        for candidate in candidates:
            if candidate.entity_id == entity_id:
                return candidate
        return None

    # --------------------------------------------------------------------- L5

    def _summarize(
        self,
        request: LocateRequest,
        mentions: Sequence[EntityMention],
        located: Sequence[LocatedEntity],
        unresolved: Sequence[str],
        traces: Sequence[dict[str, Any]],
        version: int,
        *,
        stage_ms: dict[str, float],
        total_ms: float,
    ) -> LocateResult:
        entity_ids = tuple(dict.fromkeys(item.entity_id for item in located))
        entity_ids = entity_ids[: self.max_entities]
        keep = set(entity_ids)
        located = tuple(item for item in located if item.entity_id in keep)
        layer_ms = _layer_totals(traces, stage_ms)
        return LocateResult(
            entities=located,
            scope=EntityScope(
                entity_ids=entity_ids,
                composition=_composition(request.query),
                min_mount_confidence=_min_mount_confidence(request),
                residual_query=build_residual_query(request.query, mentions, located),
                unresolved=tuple(unresolved),
            ),
            diagnostics={
                "registry_version": version,
                "mention_count": len(mentions),
                "located_count": len(located),
                "unresolved_count": len(unresolved),
                "mentions": list(traces),
                "locate_ms": total_ms,
                "layer_ms": layer_ms,
            },
        )

    def _empty_result(
        self,
        request: LocateRequest,
        version: int,
        mentions: Sequence[EntityMention],
        *,
        stage_ms: dict[str, float] | None = None,
        total_ms: float = 0.0,
    ) -> LocateResult:
        return LocateResult(
            entities=(),
            scope=EntityScope(
                entity_ids=(),
                composition=_composition(request.query),
                min_mount_confidence=_min_mount_confidence(request),
                residual_query=request.query,
                unresolved=tuple(mention.surface_form for mention in mentions),
            ),
            diagnostics={
                "registry_version": version,
                "mention_count": len(mentions),
                "located_count": 0,
                "unresolved_count": len(mentions),
                "mentions": [],
                "locate_ms": total_ms,
                "layer_ms": _layer_totals((), stage_ms or {}),
            },
        )

    @staticmethod
    def _candidate(item: dict[str, Any]) -> EntityCandidate:
        return EntityCandidate(
            entity_id=str(item["entity_id"]),
            domain=str(item.get("domain") or ""),
            canonical_name=str(item.get("canonical_name") or ""),
            match_method=str(item.get("match_method") or "exact"),
            match_score=float(item.get("match_score") or 0.0),
            registry_version=int(item.get("registry_version") or 1),
            evidence=dict(item.get("evidence") or {}),
        )


def extract_mentions(
    query: str,
    entities: Sequence[Entity],
    aliases: Sequence[EntityAlias],
) -> list[EntityMention]:
    """L0: find every mention span in the query.

    Registered names and aliases are matched longest-first on the raw text so
    the span offsets stay usable for residual-query rewriting; a match may not
    overlap one already taken. Unregistered type-suffixed candidates are added
    afterwards, and deictic phrases become mentions that L4 can resolve.
    """
    text = str(query or "")
    if not text.strip():
        return []

    entries: list[tuple[str, str, str | None]] = []
    for entity in entities:
        if entity.status != "active":
            continue
        for value in (entity.canonical_name, entity.normalized_key):
            raw = str(value or "").strip()
            if raw:
                entries.append((raw, entity.domain, None))
    for alias in aliases:
        if alias.status != "active":
            continue
        raw = str(alias.display_alias or alias.normalized_alias or "").strip()
        if raw:
            entries.append((raw, alias.domain, None))
    entries = list(dict.fromkeys(entries))
    entries.sort(key=lambda item: (-len(item[0]), item[0]))

    lowered = text.lower()
    taken: list[tuple[int, int]] = []
    mentions: list[EntityMention] = []
    for raw, domain, _ in entries:
        start = lowered.find(raw.lower())
        while start >= 0:
            end = start + len(raw)
            if not any(start < used_end and end > used_start for used_start, used_end in taken):
                break
            start = lowered.find(raw.lower(), start + 1)
        if start < 0:
            continue
        taken.append((start, start + len(raw)))
        mentions.append(
            EntityMention(
                mention_id=f"m{len(mentions)}",
                surface_form=text[start:start + len(raw)],
                normalized_form=normalized_text(raw),
                start=start,
                end=start + len(raw),
                type_hint=domain or None,
            )
        )

    # Deictic phrases are checked before the type-suffix scan: "那个项目" also
    # looks like a project name by suffix, but resolving it needs conversation
    # context, not a registry lookup.
    for phrase in _DEICTIC_PHRASES:
        start = text.find(phrase)
        if start < 0:
            continue
        end = start + len(phrase)
        if any(start < used_end and end > used_start for used_start, used_end in taken):
            continue
        taken.append((start, end))
        mentions.append(
            EntityMention(
                mention_id=f"m{len(mentions)}",
                surface_form=phrase,
                normalized_form=normalized_text(phrase),
                start=start,
                end=end,
                is_deictic=True,
            )
        )

    for name, domain in discover_candidate_names(text):
        if any(name in mention.surface_form or mention.surface_form in name for mention in mentions):
            continue
        start = text.find(name)
        if start < 0:
            continue
        end = start + len(name)
        if any(start < used_end and end > used_start for used_start, used_end in taken):
            continue
        taken.append((start, end))
        mentions.append(
            EntityMention(
                mention_id=f"m{len(mentions)}",
                surface_form=name,
                normalized_form=normalized_text(name),
                start=start,
                end=end,
                type_hint=domain,
            )
        )

    if not mentions and text.strip():
        # A bare abbreviation ("aims") matches no registry entry and carries no
        # type suffix, yet it is exactly what the semantic layer exists for. Ask
        # L3 with the whole query rather than dropping the mention entirely.
        mentions.append(
            EntityMention(
                mention_id="m0",
                surface_form=text.strip(),
                normalized_form=normalized_text(text),
                start=text.index(text.strip()),
                end=text.index(text.strip()) + len(text.strip()),
                source="query_fallback",
            )
        )

    mentions.sort(key=lambda item: item.start if item.start is not None else 0)
    return [
        EntityMention(
            mention_id=f"m{index}",
            surface_form=mention.surface_form,
            normalized_form=mention.normalized_form,
            start=mention.start,
            end=mention.end,
            type_hint=mention.type_hint,
            is_deictic=mention.is_deictic,
            source=mention.source,
        )
        for index, mention in enumerate(mentions)
    ]


def build_residual_query(
    query: str,
    mentions: Sequence[EntityMention],
    located: Sequence[LocatedEntity],
) -> str:
    """Strip resolved mention spans so the entity name does not dominate BM25.

    Only spans that were actually resolved are removed: an unresolved name is
    still useful text for the fallback full-corpus search.
    """
    resolved_ids = {item.mention_id for item in located}
    spans = [
        (mention.start, mention.end)
        for mention in mentions
        if mention.mention_id in resolved_ids
        and mention.start is not None
        and mention.end is not None
    ]
    if not spans:
        return str(query or "").strip()
    text = str(query or "")
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    residual = "".join(kept).strip()
    # An empty residual means the query *was* the entity name; searching for
    # nothing is meaningless, so fall back to the original text.
    return residual or text.strip()


def _composition(query: str) -> str:
    text = str(query or "")
    if "或" in text or "或者" in text:
        return "or"
    return "and"


def _min_mount_confidence(request: LocateRequest) -> float:
    if request.min_mount_confidence is not None:
        return float(request.min_mount_confidence)
    return 0.65
