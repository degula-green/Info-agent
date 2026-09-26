from __future__ import annotations

import unittest

from app.config import settings
from app.domain.rag import Chunk, ResourceContext
from app.infrastructure.rag_elasticsearch import RagChunkIndex


class _Indices:
    def __init__(self, client):
        self.client = client

    def exists(self, *, index):
        return index in self.client.physical

    def exists_alias(self, *, name):
        return name in self.client.aliases

    def create(self, *, index, settings, mappings):
        self.client.physical.add(index)
        self.client.mappings[index] = mappings

    def put_mapping(self, *, index, properties):
        self.client.mappings[index] = {"properties": properties}

    def update_aliases(self, *, actions):
        for action in actions:
            item = action["add"]
            self.client.aliases.add(item["alias"])


class _Client:
    def __init__(self):
        self.physical = set()
        self.aliases = set()
        self.mappings = {}
        self.bulk_calls = []
        self.indices = _Indices(self)

    def bulk(self, *, operations, refresh):
        self.bulk_calls.append(operations)
        return {"errors": False}


def _context() -> ResourceContext:
    return ResourceContext.from_event_and_source(
        {
            "resource_type": "message",
            "resource_id": "00000000-0000-0000-0000-000000000001",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000002",
            "source_audience_policy": "organization_members",
            "content_version": 1,
            "acl_version": 1,
            "content_hash": "a" * 64,
            "content_access_required": False,
        },
        {
            "scope_type": "organization",
            "scope_id": "00000000-0000-0000-0000-000000000003",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000004",
            "content_hash": "a" * 64,
        },
    )


class ElasticsearchContractTests(unittest.TestCase):
    def test_create_indices_and_index_embedding(self) -> None:
        client = _Client()
        store = RagChunkIndex(client=client)
        created = store.create_indices()
        self.assertIn("rag_chunks_display_v1", created)
        self.assertIn(settings.elasticsearch_display_read_index, client.aliases)
        chunk = Chunk.create(
            context=_context(),
            snapshot_id="snapshot",
            chunk_index=0,
            chunk_count=1,
            content="hello",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        chunk.embedding = [0.1] * settings.embedding_dims
        chunk.embedding_model = "test"
        chunk.embedding_dimensions = settings.embedding_dims
        self.assertEqual(store.index_chunks([chunk]), 1)
        self.assertEqual(len(client.bulk_calls), 1)


if __name__ == "__main__":
    unittest.main()
