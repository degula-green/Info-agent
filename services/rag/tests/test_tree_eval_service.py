"""Labelled-set importing, metric computation and run recording."""

from app.application.entity_locator import EntityLocator
from app.application.tree_eval_service import (
    TreeEvalService,
    evaluate_location_case,
    evaluate_retrieval_case,
    summarize_location,
    summarize_retrieval,
)
from app.domain.rag import normalized_text
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


def _repo_with_entities(*names: str) -> tuple[InMemoryRagMVPRepository, dict[str, str]]:
    repo = InMemoryRagMVPRepository()
    ids: dict[str, str] = {}
    for name in names:
        ids[name] = repo.upsert_entity(
            domain="project", canonical_name=name, normalized_key=normalized_text(name), **SCOPE
        )["id"]
    return repo, ids


class TestLocationMetrics:
    def test_exact_set_match_is_required(self):
        # Locating an extra entity is an error, not a near miss: it widens the
        # search scope and lets unrelated chunks through.
        assert evaluate_location_case(["a"], ["a"]) == (True, 1.0, 1.0)
        correct, recall, precision = evaluate_location_case(["a", "b"], ["a"])
        assert correct is False
        assert recall == 1.0
        assert precision == 0.5

    def test_missing_entity_lowers_recall(self):
        correct, recall, precision = evaluate_location_case(["a"], ["a", "b"])
        assert correct is False
        assert recall == 0.5
        assert precision == 1.0

    def test_no_entity_label_treats_invention_as_failure(self):
        assert evaluate_location_case([], []) == (True, 1.0, 1.0)
        correct, recall, _ = evaluate_location_case(["a"], [])
        assert correct is False
        assert recall == 0.0

    def test_summary_aggregates_and_handles_the_empty_set(self):
        empty = summarize_location([])
        assert empty["case_count"] == 0 and empty["exact_match_rate"] == 0.0

        from app.application.tree_eval_service import EvalOutcome

        summary = summarize_location([
            EvalOutcome("q1", ("a",), ("a",), True, 1.0, 1.0),
            EvalOutcome("q2", ("a",), ("a", "b"), False, 0.5, 1.0),
        ])
        assert summary["case_count"] == 2
        assert summary["exact_match_rate"] == 0.5
        assert summary["mean_recall"] == 0.75


class TestRetrievalMetrics:
    def test_recall_and_reciprocal_rank(self):
        assert evaluate_retrieval_case(["x", "y"], ["y"], k=5) == (True, 1.0, 0.5)
        correct, recall, rr = evaluate_retrieval_case(["x"], ["y"], k=5)
        assert (correct, recall, rr) == (False, 0.0, 0.0)
        # A gold chunk past k does not count.
        assert evaluate_retrieval_case(["x", "y", "z"], ["z"], k=2)[1] == 0.0

    def test_summary(self):
        summary = summarize_retrieval([(True, 1.0, 1.0), (False, 0.5, 0.0)])
        assert summary["case_count"] == 2
        assert summary["recall_at_k"] == 0.75
        assert summary["mrr"] == 0.5


class TestTreeEvalService:
    def test_import_then_run_records_metrics_and_failures(self):
        repo, ids = _repo_with_entities("A项目", "B项目")
        service = TreeEvalService(
            repository=repo, locator=EntityLocator(repository=repo)
        )
        imported = service.import_cases(
            **SCOPE, suite="entity_location", dataset_version=1,
            cases=[
                {"query": "A项目的进展", "labels": {"entity_ids": [ids["A项目"]]}},
                # Labelled with the wrong entity on purpose so the run has a
                # failure to report.
                {"query": "B项目的进展", "labels": {"entity_ids": [ids["A项目"]]}},
                # "No entity here" is a legitimate label.
                {"query": "今天天气怎么样", "labels": {"entity_ids": []}},
            ],
        )

        metrics = service.run_entity_location(**SCOPE, dataset_version=1, label="v1")

        assert imported == 3
        assert metrics["case_count"] == 3
        assert metrics["exact_match_rate"] == round(2 / 3, 4)
        assert [item["query"] for item in metrics["failures"]] == ["B项目的进展"]

        runs = repo.list_eval_runs(**SCOPE, suite="entity_location")
        assert len(runs) == 1
        assert runs[0]["label"] == "v1"
        assert runs[0]["passed_count"] == 2

    def test_import_is_idempotent_per_version(self):
        repo, ids = _repo_with_entities("A项目")
        service = TreeEvalService(repository=repo, locator=EntityLocator(repository=repo))
        payload = {"query": "A项目", "labels": {"entity_ids": [ids["A项目"]]}}

        service.import_cases(**SCOPE, suite="entity_location", dataset_version=1, cases=[payload])
        service.import_cases(**SCOPE, suite="entity_location", dataset_version=1, cases=[payload])

        # Re-import tops the set up instead of duplicating it.
        assert len(repo.list_eval_cases(**SCOPE, suite="entity_location", dataset_version=1)) == 1

    def test_versions_are_isolated(self):
        repo, ids = _repo_with_entities("A项目")
        service = TreeEvalService(repository=repo, locator=EntityLocator(repository=repo))
        for version, expected in ((1, [ids["A项目"]]), (2, [])):
            service.import_cases(
                **SCOPE, suite="entity_location", dataset_version=version,
                cases=[{"query": "A项目", "labels": {"entity_ids": expected}}],
            )

        v1 = service.run_entity_location(**SCOPE, dataset_version=1)
        v2 = service.run_entity_location(**SCOPE, dataset_version=2)

        # Freezing matters: the same query can carry a different label in a new
        # version, and each run only sees its own.
        assert v1["exact_match_rate"] == 1.0
        assert v2["exact_match_rate"] == 0.0

    def test_malformed_cases_are_skipped(self):
        repo = InMemoryRagMVPRepository()
        service = TreeEvalService(repository=repo, locator=EntityLocator(repository=repo))

        imported = service.import_cases(
            **SCOPE, suite="entity_location", dataset_version=1,
            cases=[
                {"query": "", "labels": {"entity_ids": []}},
                {"query": "no labels"},
                {"labels": {"entity_ids": []}},
            ],
        )

        assert imported == 0
