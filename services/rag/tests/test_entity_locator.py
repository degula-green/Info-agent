"""Five-layer entity location."""

from app.application.entity_locator import EntityLocator, extract_mentions
from app.domain.location import LocateRequest
from app.domain.rag import normalized_text
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


class FakeEmbedding:
    dimensions = 3

    def __init__(self, vector):
        self.vector = vector
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [list(self.vector) for _ in texts]


class FakeVerifier:
    """Returns the first candidate unless a decision is supplied."""

    def __init__(self, entity_id=None):
        self.entity_id = entity_id
        self.calls = 0

    def verify(self, *, mention, candidates, context_messages):
        self.calls += 1
        if self.entity_id is not None:
            return {"entity_id": self.entity_id, "confidence": 0.9, "reason": "test"}
        if candidates:
            return {"entity_id": candidates[0].entity_id, "confidence": 0.9, "reason": "test"}
        return None


def _entity(repo, name, domain="project", entity_id=None, registry_version=1):
    # Production stores the normalized key (the promote path passes the
    # candidate's normalized key), so the double does the same.
    return repo.upsert_entity(
        entity_id=entity_id, domain=domain, canonical_name=name,
        normalized_key=normalized_text(name),
        registry_version=registry_version, **SCOPE
    )["id"]


def _locate(repo, query, **overrides):
    request = LocateRequest(query=query, **SCOPE, **overrides)
    return EntityLocator(repository=repo).locate(request)


def test_exact_match_strips_the_name_from_the_residual_query():
    repo = InMemoryRagMVPRepository()
    entity_id = _entity(repo, "青云飞鹏")

    result = _locate(repo, "青云飞鹏的服务器配置")

    assert [item.entity_id for item in result.entities] == [entity_id]
    assert result.scope.entity_ids == (entity_id,)
    assert result.scope.residual_query == "的服务器配置"
    assert result.entities[0].match_method == "exact"


def test_multi_mention_query_resolves_every_mention():
    # The whole reason location is per mention: resolving the first entity must
    # not suppress the second one.
    repo = InMemoryRagMVPRepository()
    person = _entity(repo, "张三", domain="person")
    project = _entity(repo, "A项目", domain="project")

    result = _locate(repo, "张三在A项目的服务器")

    assert {item.entity_id for item in result.entities} == {person, project}
    assert set(result.scope.entity_ids) == {person, project}
    assert result.diagnostics["mention_count"] == 2
    assert result.diagnostics["unresolved_count"] == 0


def test_same_name_in_two_domains_returns_both():
    repo = InMemoryRagMVPRepository()
    first = _entity(repo, "张三", domain="person")
    second = _entity(repo, "张三", domain="project")

    result = _locate(repo, "张三")

    assert {item.entity_id for item in result.entities} == {first, second}


def test_longest_name_wins_over_a_shorter_registered_one():
    repo = InMemoryRagMVPRepository()
    long_id = _entity(repo, "青云飞鹏项目")
    _entity(repo, "青云")

    mentions = extract_mentions(
        "青云飞鹏项目的进度", *repo.load_entity_registry(**SCOPE)[:2]
    )

    assert [mention.surface_form for mention in mentions] == ["青云飞鹏项目"]
    assert _locate(repo, "青云飞鹏项目的进度").scope.entity_ids == (long_id,)


def test_containment_match_is_rejected_without_verification():
    # "官网项目" only appears inside "青云飞鹏官网项目": useful, but not
    # trustworthy enough to act on unverified.
    repo = InMemoryRagMVPRepository()
    _entity(repo, "青云飞鹏官网项目")

    result = _locate(repo, "官网项目的进展")

    assert result.entities == ()
    assert result.scope.unresolved == ("官网项目",)


def test_containment_match_is_accepted_when_the_verifier_confirms():
    repo = InMemoryRagMVPRepository()
    entity_id = _entity(repo, "青云飞鹏官网项目")
    request = LocateRequest(query="官网项目的进展", **SCOPE)
    verifier = FakeVerifier()

    result = EntityLocator(repository=repo, verifier=verifier).locate(request)

    assert verifier.calls == 1
    assert result.scope.entity_ids == (entity_id,)
    assert result.entities[0].verified is True


def test_deictic_mention_falls_through_to_the_verifier():
    repo = InMemoryRagMVPRepository()
    entity_id = _entity(repo, "青云飞鹏项目")
    request = LocateRequest(
        query="那个项目进展如何", **SCOPE, context_messages=("上次聊到青云飞鹏项目",)
    )
    verifier = FakeVerifier(entity_id=entity_id)

    locator = EntityLocator(repository=repo, verifier=verifier)
    mentions = extract_mentions(request.query, *repo.load_entity_registry(**SCOPE)[:2])

    assert [mention.is_deictic for mention in mentions] == [True]
    # Without a lexical hit there is nothing for the verifier to choose from,
    # so the mention stays unresolved rather than being guessed.
    result = locator.locate(request)
    assert result.scope.entity_ids == ()
    assert result.scope.unresolved == ("那个项目",)


def test_query_without_mentions_keeps_the_original_text():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "青云飞鹏")

    result = _locate(repo, "今天天气怎么样")

    assert result.entities == ()
    assert result.scope.entity_ids == ()
    assert result.scope.residual_query == "今天天气怎么样"
    # The whole query becomes one fallback mention so the semantic layer gets a
    # chance; without an embedding provider nothing resolves.
    assert result.diagnostics["mention_count"] == 1
    assert result.scope.unresolved == ("今天天气怎么样",)


def test_residual_query_falls_back_when_the_query_is_only_the_name():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "青云飞鹏")

    result = _locate(repo, "青云飞鹏")

    assert result.scope.entity_ids != ()
    # Stripping the only token would leave an empty query; keep the original.
    assert result.scope.residual_query == "青云飞鹏"


def test_or_query_sets_composition_to_or():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "A项目")
    _entity(repo, "B项目")

    assert _locate(repo, "A项目或者B项目的进展").scope.composition == "or"
    assert _locate(repo, "A项目和B项目的进展").scope.composition == "and"


def test_semantic_layer_runs_when_lexical_layers_miss():
    repo = InMemoryRagMVPRepository()
    entity_id = _entity(repo, "AIMS系统开发项目")
    repo.update_entity_embedding(
        entity_id=entity_id, embedding=[1.0, 0.0, 0.0], model="fake", dimensions=3
    )
    embedding = FakeEmbedding([1.0, 0.0, 0.0])
    request = LocateRequest(query="aims", **SCOPE)

    result = EntityLocator(repository=repo, embedding=embedding).locate(request)

    assert embedding.calls == 1
    assert result.scope.entity_ids == (entity_id,)
    assert result.entities[0].match_method == "semantic"


def test_location_reports_a_number_for_every_layer_it_ran():
    repo = InMemoryRagMVPRepository()
    _entity(repo, "青云飞鹏")

    result = _locate(repo, "青云飞鹏的服务器配置")

    layers = result.diagnostics["layer_ms"]
    # L0 covers the registry read plus mention extraction, so it always runs.
    assert layers["L0"] >= 0.0
    assert layers["L1"] >= 0.0
    assert result.diagnostics["locate_ms"] >= layers["L0"]
    trace = result.diagnostics["mentions"][0]["layer_trace"]
    assert [entry["layer"] for entry in trace] == ["L1"]
    assert trace[0]["elapsed_ms"] >= 0.0


def test_layer_time_sums_across_mentions():
    # Location is per mention, so a two-mention query does two L1 lookups. The
    # reported figure has to be the query's cost, not one mention's.
    repo = InMemoryRagMVPRepository()
    _entity(repo, "张三", domain="person")
    _entity(repo, "A项目")

    result = _locate(repo, "张三在A项目")

    entries = [
        entry
        for trace in result.diagnostics["mentions"]
        for entry in trace["layer_trace"]
        if entry["layer"] == "L1"
    ]
    assert len(entries) == 2
    assert result.diagnostics["layer_ms"]["L1"] == round(
        sum(entry["elapsed_ms"] for entry in entries), 3
    )


class _SemanticOnlyRepository:
    """Registry is empty; only the semantic layer answers, with fixed scores."""

    def __init__(self, scores):
        self.scores = scores

    def load_entity_registry(self, *, scope_type, scope_id):
        return [], [], 1

    def locate_entities_exact(self, *, scope_type, scope_id, normalized):
        return []

    def locate_entities_fuzzy(self, *, scope_type, scope_id, normalized):
        return []

    def locate_entities_semantic(self, *, scope_type, scope_id, embedding, limit):
        return [
            {
                "entity_id": f"e{index}",
                "domain": "project",
                "canonical_name": f"候选{index}",
                "match_method": "semantic",
                "match_score": score,
                "registry_version": 1,
            }
            for index, score in enumerate(self.scores, start=1)
        ]


class _NoAnswerVerifier:
    """Models the timeout / refusal path: no verdict at all."""

    def __init__(self):
        self.calls = 0

    def verify(self, *, mention, candidates, context_messages):
        self.calls += 1
        return None


def _locate_with_candidates(scores, *, min_confidence=0.5):
    # A gap under 0.08 forces the L4 gate even when the top score is high.
    locator = EntityLocator(
        repository=_SemanticOnlyRepository(scores),
        embedding=FakeEmbedding([1.0, 0.0, 0.0]),
        verifier=_NoAnswerVerifier(),
    )
    request = LocateRequest(
        query="青云项目进展", **SCOPE, min_confidence=min_confidence
    )
    return locator, locator.locate(request)


def test_degraded_verification_keeps_a_strong_lexical_match():
    locator, result = _locate_with_candidates([0.90, 0.85])

    assert locator.verifier.calls == 1
    assert result.scope.entity_ids == ("e1",)
    assert result.entities[0].verified is False
    trace = result.diagnostics["mentions"][0]
    assert trace["llm_degraded"] is True


def test_degraded_verification_drops_a_weak_lexical_match():
    locator, result = _locate_with_candidates([0.60, 0.56])

    assert locator.verifier.calls == 1
    # Below the fallback score the mention stays unresolved: a guess would be
    # worse than not narrowing, because the tree would then hide the answer.
    assert result.scope.entity_ids == ()
    assert result.diagnostics["mentions"][0].get("llm_degraded") is None
