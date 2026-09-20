from __future__ import annotations

import unittest

from app.application.memory.pipeline import MemoryPipeline
from app.domain.memory import FactCandidate, MemoryGraph, SourceRouteCandidate, TreeSearchRequest
from app.domain.models import AttachmentContext, ChunkRecord
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.memory_models import DeterministicFactExtractor, DeterministicNodeSummarizer, MemoryModelUnavailable, parse_fact_response
from app.infrastructure.memory_elasticsearch import ElasticsearchMemoryStore
from app.infrastructure.persistence.repository import InMemoryRagRepository


def context(version: int = 1) -> AttachmentContext:
    return AttachmentContext(
        attachment_id="00000000-0000-0000-0000-000000000041", file_name="fixture.txt", mime_type="text/plain",
        knowledge_item_id="00000000-0000-0000-0000-000000000021", knowledge_base_id="00000000-0000-0000-0000-000000000031",
        organization_id="00000000-0000-0000-0000-000000000011", conversation_group_id="session-1", sent_at="2026-09-16T08:00:00Z", content_version=version,
    )


def chunk(version: int = 1, suffix: str = "a") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"chunk-{version}-{suffix}", chunk_index=0, chunk_count=1, chunking_version="v1",
        knowledge_item_id=context().knowledge_item_id or "", attachment_id=context().attachment_id,
        knowledge_base_id=context().knowledge_base_id, content_version=version, attachment_content_version=version,
        auth_acl_version=1, mapping_version="v1", auth_resource_type="attachment", auth_resource_part="metadata",
        auth_resource_id=context().attachment_id, auth_object_key=f"attachment_meta:{context().attachment_id}", knowledge_scope=None,
        access_scope=None, organization_id=context().organization_id, owner_user_id=None, conversation_group_id="session-1",
        part_kind="message_display", content_access_required=False, title="项目", content="星河项目已进入交付阶段。",
        content_hash="0" * 64, content_visibility="display", rag_eligible=True,
    )


def fact(text: str = "星河项目已进入交付阶段", chunk_id: str = "chunk-1-a") -> FactCandidate:
    return FactCandidate(fact_type="state", text=text, subject="星河项目", entity_type="project", predicate="phase", normalized_value={"phase": "delivery"}, topic="进度", phase="交付", confidence=0.98, chunk_ids=(chunk_id,))


class _Indexer:
    def __init__(self):
        self.graphs = []

    def index_graph(self, graph, vectors):
        self.graphs.append((graph, vectors))
        return len(graph.nodes) + len(graph.fact_projections)


class _RouteOnlyExtractor:
    def extract(self, chunks):
        return []

    def source_routes(self, chunks):
        return [SourceRouteCandidate(subject="示例公司", entity_type="organization", topic="人事政策", relation_type="policy", confidence=0.9)]


class MemoryTreeTests(unittest.TestCase):
    def test_fact_json_is_strict(self):
        response = {"choices": [{"message": {"content": '{"facts":[{"fact_type":"state","text":"已交付","subject":"项目","chunk_indexes":[0]}]}'}}]}
        self.assertEqual(parse_fact_response(response, [chunk()])[0].chunk_ids, ("chunk-1-a",))
        with self.assertRaises(MemoryModelUnavailable):
            parse_fact_response({"choices": [{"message": {"content": "not-json"}}]}, [chunk()])

    def test_fact_is_deduplicated_and_mounted_in_session_and_entity_trees(self):
        repository = InMemoryRagRepository()
        first = repository.upsert_memory(context(), [chunk()], [fact()])
        second = repository.upsert_memory(context(), [chunk()], [fact()])
        self.assertEqual([row["fact_projection_id"] for row in first.fact_projections], [row["fact_projection_id"] for row in second.fact_projections])
        self.assertEqual({row["tree_type"] for row in first.fact_projections}, {"session", "entity"})
        self.assertTrue(all(len([node for node in first.nodes if node["tree_id"] == row["tree_id"]]) == 3 for row in first.fact_projections))

    def test_one_fact_keeps_multiple_chunk_evidence(self):
        repository = InMemoryRagRepository()
        candidate = FactCandidate(**{**fact().__dict__, "chunk_ids": ("chunk-1-a", "chunk-1-b")})
        graph = repository.upsert_memory(context(), [chunk(suffix="a"), chunk(suffix="b")], [candidate])
        evidence = repository.evidence_for_facts([graph.fact_projections[0]["fact_id"]])
        self.assertEqual(len(next(iter(evidence.values()))), 2)

    def test_pipeline_summarizes_embeds_and_indexes(self):
        repository, indexer = InMemoryRagRepository(), _Indexer()
        pipeline = MemoryPipeline(extractor=DeterministicFactExtractor([fact()]), summarizer=DeterministicNodeSummarizer(), repository=repository, indexer=indexer, embedding_provider=HashEmbeddingProvider(dimensions=8))
        stages = []
        graph = pipeline.process(context(), [chunk()], stage=stages.append)
        self.assertIsNotNone(graph)
        self.assertEqual(stages, ["fact", "tree", "summary", "memory_index"])
        self.assertEqual(len(indexer.graphs), 1)
        self.assertTrue(all("星河项目已进入交付阶段" in node["summary"] for node in graph.nodes))

    def test_document_without_fact_is_mounted_as_source(self):
        repository, indexer = InMemoryRagRepository(), _Indexer()
        pipeline = MemoryPipeline(extractor=_RouteOnlyExtractor(), summarizer=DeterministicNodeSummarizer(), repository=repository, indexer=indexer, embedding_provider=HashEmbeddingProvider(dimensions=8))
        graph = pipeline.process(context(), [chunk()])
        self.assertIsNotNone(graph)
        self.assertEqual(graph.fact_projections, ())
        self.assertEqual(len(graph.source_projections), 2)
        self.assertEqual({source["tree_type"] for source in graph.source_projections}, {"session", "entity"})
        for source in graph.source_projections:
            self.assertEqual(source["relation_type"], "policy")
            self.assertEqual(source["attachment_id"], context().attachment_id)
            self.assertEqual(repository.sources_for_nodes([source["node_id"]])[source["node_id"]][0]["source_id"], graph.source_id)

    def test_summary_failure_does_not_index_and_can_retry(self):
        class Broken:
            def summarize(self, **_):
                raise MemoryModelUnavailable("timeout")

        repository, indexer = InMemoryRagRepository(), _Indexer()
        pipeline = MemoryPipeline(extractor=DeterministicFactExtractor([fact()]), summarizer=Broken(), repository=repository, indexer=indexer, embedding_provider=HashEmbeddingProvider(dimensions=8))
        with self.assertRaises(MemoryModelUnavailable):
            pipeline.process(context(), [chunk()])
        self.assertTrue(repository.memory_plans)
        self.assertFalse(indexer.graphs)

    def test_repeated_version_changes_fact_version_projection(self):
        repository = InMemoryRagRepository()
        old = repository.upsert_memory(context(1), [chunk(1)], [fact()])
        changed = fact("星河项目确认已进入交付阶段", "chunk-2-a")
        new = repository.upsert_memory(context(2), [chunk(2)], [changed])
        self.assertEqual(old.fact_projections[0]["fact_id"], new.fact_projections[0]["fact_id"])
        self.assertNotEqual(old.fact_projections[0]["fact_version_id"], new.fact_projections[0]["fact_version_id"])

    def test_repeated_pipeline_is_idempotent_for_projection_records(self):
        repository, indexer = InMemoryRagRepository(), _Indexer()
        pipeline = MemoryPipeline(extractor=DeterministicFactExtractor([fact()]), summarizer=DeterministicNodeSummarizer(), repository=repository, indexer=indexer, embedding_provider=HashEmbeddingProvider(dimensions=8))
        pipeline.process(context(), [chunk()])
        first_count = len(repository.index_records)
        pipeline.process(context(), [chunk()])
        self.assertEqual(len(repository.index_records), first_count)

    def test_es_projection_and_layered_tree_search(self):
        class FakeClient:
            def __init__(self):
                self.operations = []

            def bulk(self, *, operations, refresh):
                self.operations = operations
                return {"errors": False}

            def search(self, **kwargs):
                filters = kwargs["query"]["bool"]["filter"]
                terms = {next(iter(item["term"])): next(iter(item["term"].values())) for item in filters if "term" in item}
                if "facts" in kwargs["index"]:
                    source = {"fact_id": "f1", "fact_text": "完成验收", "node_id": "leaf", "tree_type": "session"}
                elif terms.get("node_type") == "root":
                    source = {"node_id": "root", "tree_id": "tree", "tree_type": "session", "subject_key": "s", "node_type": "root", "summary": "项目"}
                elif terms.get("parent_id") == "root":
                    source = {"node_id": "group", "tree_id": "tree", "tree_type": "session", "node_type": "internal", "summary": "2026-09"}
                else:
                    source = {"node_id": "leaf", "tree_id": "tree", "tree_type": "session", "node_type": "leaf", "summary": "验收"}
                return {"hits": {"hits": [{"_score": 1.0, "_source": source}]}}

        client = FakeClient()
        store = ElasticsearchMemoryStore(client=client)
        graph = MemoryGraph("source", context().knowledge_item_id or "", ({"node_id": "root", "visibility": "display", "summary": "项目"},), ({"fact_projection_id": "projection", "fact_id": "f1", "visibility": "display", "fact_text": "完成验收"},))
        self.assertEqual(store.index_graph(graph, {"root": [0.0], "projection": [0.0]}), 2)
        self.assertEqual(len(client.operations), 4)
        rows = store.search_tree(TreeSearchRequest(query="验收", user_id="u", organization_id=context().organization_id, knowledge_base_id=context().knowledge_base_id or ""), None)
        self.assertEqual([part["node_type"] for part in rows[0]["path"]], ["root", "internal", "leaf"])
        self.assertEqual(rows[0]["fact"]["fact_id"], "f1")


if __name__ == "__main__":
    unittest.main()
