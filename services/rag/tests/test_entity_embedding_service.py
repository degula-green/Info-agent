"""Entity embedding backfill."""

from app.application.entity_embedding_service import (
    EntityEmbeddingService,
    entity_embedding_text,
)
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


class FakeEmbedding:
    model = "fake-embed"
    dimensions = 3

    def __init__(self, vectors=None, fail=False):
        self.vectors = vectors
        self.fail = fail
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("provider down")
        if self.vectors is not None:
            return self.vectors
        return [[1.0, 0.0, 0.0] for _ in texts]


def _entity(repo, name, domain="project"):
    return repo.upsert_entity(
        domain=domain, canonical_name=name, normalized_key=name, **SCOPE
    )["id"]


def test_backfill_marks_pending_entities_ready():
    repo = InMemoryRagMVPRepository()
    entity_id = _entity(repo, "青云飞鹏项目")
    embedding = FakeEmbedding()

    updated = EntityEmbeddingService(repository=repo, embedding=embedding).run_once()

    assert updated == 1
    stored = repo.entities[0]
    assert stored["embedding_status"] == "ready"
    assert stored["embedding_model"] == "fake-embed"
    assert stored["embedding"] == [1.0, 0.0, 0.0]
    # Nothing is pending any more, so a second sweep is a no-op.
    assert EntityEmbeddingService(repository=repo, embedding=embedding).run_once() == 0


def test_provider_failure_leaves_the_entity_pending():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "青云飞鹏项目")

    updated = EntityEmbeddingService(
        repository=repo, embedding=FakeEmbedding(fail=True)
    ).run_once()

    assert updated == 0
    assert repo.entities[0].get("embedding_status", "pending") == "pending"


def test_vector_count_mismatch_is_rejected():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "A项目")
    _entity(repo, "B项目")

    updated = EntityEmbeddingService(
        repository=repo, embedding=FakeEmbedding(vectors=[[1.0, 0.0, 0.0]])
    ).run_once()

    assert updated == 0


def test_entity_text_includes_aliases_and_description():
    # The alias is the only place a surface form like "aims" exists, so it has to
    # be part of the embedded text.
    text = entity_embedding_text({
        "canonical_name": "AIMS系统开发项目",
        "description": "内部研发系统",
        "keywords": "aims 研发",
        "aliases": "aims 爱慕斯",
    })

    assert "AIMS系统开发项目" in text
    assert "aims" in text
    assert "内部研发系统" in text
