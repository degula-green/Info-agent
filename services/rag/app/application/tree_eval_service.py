"""Offline evaluation for the tree RAG layers.

The labelled sets are frozen (see the eval_cases migration): the same query
keeps pointing at the same gold answer while prompts, models and thresholds
change. Without that, a metric movement cannot be attributed to anything.

Two suites are implemented here:

* ``entity_location``  query -> the entities that should be located
* ``retrieval``        query -> the chunks that should be retrieved

The ``mount`` suite (window -> entities) needs the heaviest labelling work and
is not implemented yet; the tables already accept it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.domain.location import LocateRequest


@dataclass(frozen=True)
class EvalOutcome:
    query: str
    predicted: tuple[str, ...]
    expected: tuple[str, ...]
    correct: bool
    recall: float
    precision: float


def evaluate_location_case(
    predicted: Iterable[str], expected: Iterable[str]
) -> tuple[bool, float, float]:
    """Compare a located entity set against the labelled one.

    Set equality decides correctness: locating an extra entity is a real error,
    not a near miss, because the extra entity widens the search scope and lets
    unrelated chunks in.
    """
    got = {value for value in predicted if value}
    want = {value for value in expected if value}
    if not want:
        # "No entity here" is a legitimate label; inventing one is a failure.
        return (not got), (1.0 if not got else 0.0), (1.0 if not got else 0.0)
    overlap = len(got & want)
    recall = overlap / len(want)
    precision = overlap / len(got) if got else 0.0
    return (got == want), recall, precision


def summarize_location(outcomes: Sequence[EvalOutcome]) -> dict[str, Any]:
    total = len(outcomes)
    if not total:
        return {
            "case_count": 0, "exact_match_rate": 0.0,
            "mean_recall": 0.0, "mean_precision": 0.0,
        }
    return {
        "case_count": total,
        "exact_match_rate": round(sum(1 for item in outcomes if item.correct) / total, 4),
        "mean_recall": round(sum(item.recall for item in outcomes) / total, 4),
        "mean_precision": round(sum(item.precision for item in outcomes) / total, 4),
    }


def evaluate_retrieval_case(
    ranked_ids: Sequence[str], gold_ids: Iterable[str], *, k: int
) -> tuple[bool, float, float]:
    """recall@k and reciprocal rank for one query."""
    gold = {value for value in gold_ids if value}
    if not gold:
        return (not ranked_ids[:k]), (1.0 if not ranked_ids[:k] else 0.0), 0.0
    top = list(ranked_ids[:k])
    hits = [value for value in top if value in gold]
    recall = len(set(hits)) / len(gold)
    rank = next((index for index, value in enumerate(top, start=1) if value in gold), None)
    return (recall >= 1.0), recall, (1.0 / rank if rank else 0.0)


def summarize_retrieval(outcomes: Sequence[tuple[bool, float, float]]) -> dict[str, Any]:
    total = len(outcomes)
    if not total:
        return {"case_count": 0, "recall_at_k": 0.0, "mrr": 0.0}
    return {
        "case_count": total,
        "recall_at_k": round(sum(item[1] for item in outcomes) / total, 4),
        "mrr": round(sum(item[2] for item in outcomes) / total, 4),
    }


class TreeEvalService:
    """Run a labelled suite against the live locator / retrieval service."""

    def __init__(
        self,
        *,
        repository: Any,
        locator: Any | None = None,
        retrieval: Any | None = None,
    ) -> None:
        self.repository = repository
        self.locator = locator
        self.retrieval = retrieval

    def import_cases(
        self,
        *,
        scope_type: str,
        scope_id: str,
        suite: str,
        dataset_version: int,
        cases: Sequence[dict[str, Any]],
        created_by: str | None = None,
    ) -> int:
        """Load a labelled set from a JSON document.

        Import is idempotent per (suite, version, query) so re-running a
        half-finished import tops the set up instead of duplicating it.
        """
        imported = 0
        for case in cases:
            query = str(case.get("query") or "").strip()
            labels = case.get("labels")
            if not query or not isinstance(labels, dict):
                continue
            self.repository.upsert_eval_case(
                scope_type=scope_type,
                scope_id=scope_id,
                suite=suite,
                dataset_version=int(dataset_version),
                query=query,
                labels=labels,
                notes=case.get("notes"),
                created_by=created_by,
            )
            imported += 1
        return imported

    def run_entity_location(
        self,
        *,
        scope_type: str,
        scope_id: str,
        dataset_version: int,
        label: str | None = None,
    ) -> dict[str, Any]:
        if self.locator is None:
            raise RuntimeError("a locator is required for the entity_location suite")
        cases = self.repository.list_eval_cases(
            scope_type=scope_type, scope_id=scope_id,
            suite="entity_location", dataset_version=int(dataset_version),
        )
        outcomes: list[EvalOutcome] = []
        for case in cases:
            result = self.locator.locate(LocateRequest(
                scope_type=scope_type, scope_id=scope_id, query=case["query"],
            ))
            predicted = tuple(result.scope.entity_ids)
            expected = tuple(case["labels"].get("entity_ids") or ())
            correct, recall, precision = evaluate_location_case(predicted, expected)
            outcomes.append(EvalOutcome(
                query=case["query"], predicted=predicted, expected=expected,
                correct=correct, recall=recall, precision=precision,
            ))
        metrics = summarize_location(outcomes)
        metrics["failures"] = [
            {"query": item.query, "predicted": list(item.predicted), "expected": list(item.expected)}
            for item in outcomes if not item.correct
        ][:20]
        self.repository.record_eval_run(
            scope_type=scope_type, scope_id=scope_id, suite="entity_location",
            dataset_version=int(dataset_version),
            case_count=metrics["case_count"],
            passed_count=sum(1 for item in outcomes if item.correct),
            metrics=metrics, label=label,
        )
        return metrics

    def run_retrieval(
        self,
        *,
        scope_type: str,
        scope_id: str,
        user_id: str,
        dataset_version: int,
        k: int = 10,
        label: str | None = None,
        entry: str = "global",
    ) -> dict[str, Any]:
        if self.retrieval is None:
            raise RuntimeError("a retrieval service is required for the retrieval suite")
        cases = self.repository.list_eval_cases(
            scope_type=scope_type, scope_id=scope_id,
            suite="retrieval", dataset_version=int(dataset_version),
        )
        from app.domain.rag import SearchRequest  # local import keeps the module light

        outcomes: list[tuple[bool, float, float]] = []
        failures: list[dict[str, Any]] = []
        for case in cases:
            response = self.retrieval.search(SearchRequest(
                query=case["query"], user_id=user_id, scope_type=scope_type,
                scope_id=scope_id, entry=entry, top_k=k,
            ))
            ranked = [item.chunk_id for item in response.results]
            gold = case["labels"].get("chunk_ids") or ()
            outcome = evaluate_retrieval_case(ranked, gold, k=k)
            outcomes.append(outcome)
            if not outcome[0]:
                failures.append({"query": case["query"], "ranked": ranked[:k], "gold": list(gold)})
        metrics = summarize_retrieval(outcomes)
        metrics["k"] = int(k)
        metrics["failures"] = failures[:20]
        self.repository.record_eval_run(
            scope_type=scope_type, scope_id=scope_id, suite="retrieval",
            dataset_version=int(dataset_version),
            case_count=metrics["case_count"],
            passed_count=sum(1 for item in outcomes if item[0]),
            metrics=metrics, label=label,
        )
        return metrics
