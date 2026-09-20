from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.config import settings
from app.domain.memory import MemoryGraph, SourceRouteCandidate
from app.domain.models import AttachmentContext, ChunkRecord


class MemoryPipeline:
    def __init__(self, *, extractor: Any, summarizer: Any, repository: Any, indexer: Any, embedding_provider: Any) -> None:
        self.extractor = extractor
        self.summarizer = summarizer
        self.repository = repository
        self.indexer = indexer
        self.embedding_provider = embedding_provider

    def process(self, context: AttachmentContext, chunks: list[ChunkRecord], *, stage: Any | None = None, processing_status: str = "ready", processing_error: dict[str, Any] | None = None) -> MemoryGraph | None:
        eligible = [chunk for chunk in chunks if chunk.rag_eligible]
        if stage and eligible:
            stage("fact")
        facts = self.extractor.extract(eligible) if eligible else []
        route_method = getattr(self.extractor, "source_routes", None)
        routes = route_method(eligible) if callable(route_method) else []
        if not routes:
            routes = _routes_from_facts(facts)
        if not routes:
            routes = [SourceRouteCandidate(
                subject=context.organization_id or context.owner_user_id or "company",
                entity_type="organization",
                topic=context.title or context.file_name or "company information",
                relation_type="reference",
                confidence=0.5,
            )]
        if stage:
            stage("tree")
        graph = self.repository.upsert_memory(
            context, chunks, facts, routes=routes,
            processing_status=processing_status, processing_error=processing_error,
        )
        if not eligible:
            return graph
        if stage:
            stage("summary")
        summaries = self._summaries(graph)
        updated = self.repository.update_node_summaries(summaries, strategy_version=settings.memory_summary_strategy_version)
        if updated:
            by_id = {node["node_id"]: node for node in graph.nodes}
            by_id.update({node["node_id"]: node for node in updated})
            graph = replace(graph, nodes=tuple(by_id.values()))
        texts = [str(node.get("summary") or "") for node in graph.nodes] + [str(fact["fact_text"]) for fact in graph.fact_projections]
        vectors = self.embedding_provider.embed(texts)
        embedding_model = getattr(self.embedding_provider, "model", settings.embedding_model)
        for document in (*graph.nodes, *graph.fact_projections):
            document["embedding_model"] = embedding_model
        keys = [node["node_id"] for node in graph.nodes] + [fact["fact_projection_id"] for fact in graph.fact_projections]
        if stage:
            stage("memory_index")
        self.indexer.index_graph(graph, dict(zip(keys, vectors)))
        record = getattr(self.repository, "record_memory_projections", None)
        if callable(record):
            record(graph, embedding_model=embedding_model)
        return graph

    def _summaries(self, graph: MemoryGraph) -> dict[str, str]:
        facts_by_leaf: dict[str, list[str]] = {}
        for fact in graph.fact_projections:
            facts_by_leaf.setdefault(str(fact["node_id"]), []).append(str(fact["fact_text"]))
        children: dict[str, list[str]] = {}
        for node in graph.nodes:
            if node.get("parent_id"):
                children.setdefault(str(node["parent_id"]), []).append(str(node["node_id"]))

        def collect(node_id: str) -> list[str]:
            values = list(facts_by_leaf.get(node_id, []))
            for child in children.get(node_id, []):
                values.extend(collect(child))
            return list(dict.fromkeys(values))

        return {
            str(node["node_id"]): self.summarizer.summarize(title=str(node.get("summary") or node.get("subject_key") or "general"), facts=collect(str(node["node_id"])))
            for node in graph.nodes
        }


def _routes_from_facts(facts: list[Any]) -> list[SourceRouteCandidate]:
    routes: list[SourceRouteCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        key = (fact.subject, fact.entity_type, fact.topic, fact.phase)
        if key in seen:
            continue
        seen.add(key)
        routes.append(SourceRouteCandidate(
            subject=fact.subject,
            entity_type=fact.entity_type,
            topic=fact.topic,
            phase=fact.phase,
            relation_type="evidence",
            confidence=fact.confidence,
        ))
    return routes
