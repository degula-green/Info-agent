"""Create/update the isolated tree-RAG demo and verify layered retrieval."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv
import psycopg

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
load_dotenv(SERVICE_ROOT / ".env", override=False)

from app.application.memory.pipeline import MemoryPipeline
from app.application.processing.chunking import build_chunks
from app.domain.memory import FactCandidate, TreeSearchRequest
from app.domain.models import AttachmentContext, CanonicalBlock, ParsedDocument
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.elasticsearch import ElasticsearchChunkStore
from app.infrastructure.memory_elasticsearch import ElasticsearchMemoryStore
from app.infrastructure.memory_models import DeterministicFactExtractor, DeterministicNodeSummarizer
from app.infrastructure.persistence.repository import PostgresRagRepository
from app.services.memory_search_service import TreeSearchService

ORG = "00000000-0000-0000-0000-00000000d001"
KB = "00000000-0000-0000-0000-00000000d002"
ITEM = "00000000-0000-0000-0000-00000000d003"
ATTACHMENT = "00000000-0000-0000-0000-00000000d004"
MESSAGE = "00000000-0000-0000-0000-00000000d005"
USER = "00000000-0000-0000-0000-00000000d006"


def main() -> None:
    database_url = os.environ["RAG_DATABASE_URL"]
    repository = PostgresRagRepository(connection_factory=lambda: psycopg.connect(database_url))
    store = ElasticsearchMemoryStore()
    store.create_indices()
    context = AttachmentContext(
        attachment_id=ATTACHMENT, file_name="tree-rag-demo.txt", mime_type="text/plain",
        source_content_hash="1" * 64, organization_id=ORG, knowledge_item_id=ITEM,
        knowledge_base_id=KB, conversation_group_id="tree-rag-demo-session", message_id=MESSAGE,
        sent_at="2026-09-16T10:00:00+08:00", title="树形RAG演示", part_kind="message_display",
    )
    parsed = ParsedDocument(
        markdown="星河项目已进入交付阶段，计划在2026年9月30日前完成验收。",
        blocks=[CanonicalBlock(page_number=None, order=0, type="text", text="星河项目已进入交付阶段，计划在2026年9月30日前完成验收。", heading_path=("星河项目",))],
        parser="fixture", parser_version="v1",
    )
    embedding = HashEmbeddingProvider(dimensions=1536)
    chunk = build_chunks(parsed, context)[0]
    chunk = replace(chunk, embedding=embedding.embed([chunk.content])[0], embedding_model=embedding.model, vectorized=True)
    ElasticsearchChunkStore().index_chunks([chunk])
    facts = [
        FactCandidate(fact_type="state", text="星河项目已进入交付阶段", subject="星河项目", entity_type="project", predicate="phase", normalized_value={"phase": "delivery"}, topic="项目进度", phase="交付", occurred_at="2026-09-16T10:00:00+08:00", confidence=.99, chunk_ids=(chunk.chunk_id,)),
        FactCandidate(fact_type="task", text="星河项目计划在2026年9月30日前完成验收", subject="星河项目", entity_type="project", predicate="deadline", normalized_value={"date": "2026-09-30"}, topic="项目验收", phase="交付", occurred_at="2026-09-30T00:00:00+08:00", confidence=.99, chunk_ids=(chunk.chunk_id,)),
    ]
    graph = MemoryPipeline(extractor=DeterministicFactExtractor(facts), summarizer=DeterministicNodeSummarizer(), repository=repository, indexer=store, embedding_provider=embedding).process(context, [chunk])
    result = TreeSearchService(indexer=store, repository=repository, embedding_provider=embedding).search(TreeSearchRequest(query="星河项目什么时候完成验收", user_id=USER, organization_id=ORG, knowledge_base_id=KB, top_k=5))
    print(json.dumps({"source_id": graph.source_id if graph else None, "nodes": len(graph.nodes) if graph else 0, "fact_projections": len(graph.fact_projections) if graph else 0, "search_items": len(result["items"]), "tree_types": sorted({item["tree"]["tree_type"] for item in result["items"]})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
