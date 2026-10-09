"""Zero-annotation tree metrics and their guard-rail alerts."""

from app.application.tree_metrics_service import (
    TreeMetricsService,
    evaluate_alerts,
    flatten_metrics,
    render_prometheus,
    summarize_scan_runs,
    summarize_search,
)
from app.domain.rag import Chunk, EntityMount, normalized_text
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


def _chunk(index: int) -> Chunk:
    return Chunk(
        chunk_id=f"{index:064d}",
        resource_snapshot_id="00000000-0000-0000-0000-000000000001",
        knowledge_item_id="00000000-0000-0000-0000-000000000002",
        resource_type="message",
        resource_id=f"00000000-0000-0000-0000-{index:012d}",
        knowledge_base_id="00000000-0000-0000-0000-000000000003",
        scope_type=SCOPE["scope_type"],
        scope_id=SCOPE["scope_id"],
        content_version=1,
        processing_version="v1",
        chunking_version="v1",
        content_variant="display",
        chunk_index=index,
        chunk_count=1,
        content=f"消息 {index}",
        content_hash="0" * 64,
        sent_at=f"2026-10-01T00:{index:02d}:00Z",
        source_conversation_id="conv-1",
    )


def test_empty_scope_reports_zero_coverage_without_misleading_alerts():
    repo = InMemoryRagMVPRepository()

    snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

    assert snapshot["mount_coverage"] == 0.0
    assert snapshot["entity_count"] == 0
    # No entities means "no mounts" is expected, not a fault: the tree has not
    # been populated yet.
    assert snapshot["alerts"] == []


def test_coverage_counts_distinct_mounted_messages():
    repo = InMemoryRagMVPRepository()
    chunks = [_chunk(i) for i in range(1, 5)]
    repo.upsert_chunks(chunks)
    entity_id = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]
    repo.merge_chunk_mounts(chunks[0], [EntityMount(entity_id, "project", 1, "window_batch", 0.85)])
    repo.merge_chunk_mounts(chunks[1], [EntityMount(entity_id, "project", 1, "window_batch", 0.85)])

    snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

    assert snapshot["message_count"] == 4
    assert snapshot["mounted_chunk_count"] == 2
    assert snapshot["mount_count"] == 2
    assert snapshot["mount_coverage"] == 0.5
    assert snapshot["mount_methods"]["window_batch"]["count"] == 2
    assert snapshot["mount_methods"]["window_batch"]["avg_confidence"] == 0.85


def test_entities_without_mounts_raise_the_no_mounts_alert():
    repo = InMemoryRagMVPRepository()
    repo.upsert_chunks([_chunk(1)])
    repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )
    repo.update_entity_embedding(
        entity_id=repo.entities[0]["id"], embedding=[1.0], model="fake", dimensions=1
    )

    alerts = TreeMetricsService(repository=repo).snapshot(**SCOPE)["alerts"]

    assert "no_mounts" in alerts


def test_candidate_backlog_and_missing_embeddings_are_flagged():
    alerts = evaluate_alerts({
        "message_count": 10,
        "entity_count": 10,
        "mount_count": 5,
        "relation_count": 1,
        "pending_candidate_count": 150,
        "entities_missing_embedding": 5,
    })

    assert "candidate_backlog" in alerts
    # Half the entities have no vector, so L3 cannot see them.
    assert "entities_missing_embedding" in alerts
    assert "no_mounts" not in alerts


def test_missing_relations_is_reported_separately():
    alerts = evaluate_alerts({
        "message_count": 10,
        "entity_count": 2,
        "mount_count": 4,
        "relation_count": 0,
        "pending_candidate_count": 0,
        "entities_missing_embedding": 0,
    })

    assert alerts == ["no_relations"]


class TestSearchMetrics:
    def test_empty_window_reports_zeros(self):
        summary = summarize_search([])

        assert summary["query_count"] == 0
        assert summary["no_entity_match_rate"] == 0.0
        assert summary["l4_invocation_rate"] == 0.0
        assert summary["latency_ms"] == {"p50": 0, "p95": 0, "max": 0, "sample_count": 0}
        assert summary["locate_latency_ms"]["sample_count"] == 0
        assert summary["locate_escalated_ms"]["sample_count"] == 0

    def test_rates_and_reason_buckets(self):
        rows = [
            {"execution_path": "tree", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": False, "resolved_entity_count": 2,
             "locate_ms": 90.0, "duration_ms": 40},
            {"execution_path": "tree", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": True, "resolved_entity_count": 1,
             "locate_ms": 700.0, "duration_ms": 900},
            # A query with no entity match degrades to the traditional path.
            {"execution_path": "traditional", "fallback_reason": "no_entity_match",
             "degraded_reason": "embedding_failed", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 120.0, "duration_ms": 120},
            {"execution_path": "traditional", "fallback_reason": "no_entity_match",
             "degraded_reason": "branch_failed,embedding_failed", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 150.0, "duration_ms": 200},
        ]

        summary = summarize_search(rows, window_hours=24)

        assert summary["query_count"] == 4
        assert summary["execution_paths"] == {"tree": 2, "traditional": 2}
        assert summary["fallback_reasons"] == {"no_entity_match": 2}
        # Comma-separated degradation reasons are split so each can be counted.
        assert summary["degraded_reasons"] == {"embedding_failed": 2, "branch_failed": 1}
        assert summary["resolved_entity_rate"] == 0.5
        assert summary["no_entity_match_rate"] == 0.5
        assert summary["l4_invocation_rate"] == 0.25
        assert summary["latency_ms"]["max"] == 900
        assert summary["latency_ms"]["p95"] >= 200

    def test_scope_exports_do_not_dilute_the_rates(self):
        # Live data had one export-only build writing 93% of the rows, which
        # made the no-entity-match rate read 16.9% while every retrieval that
        # did run resolved nothing. Exports are counted, not averaged over.
        exports = [
            {"execution_path": "scope_export", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "duration_ms": 5000}
            for _ in range(90)
        ]
        retrievals = [
            {"execution_path": "tree_shadow", "fallback_reason": "no_entity_match",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 500.0, "duration_ms": 60}
            for _ in range(10)
        ]

        summary = summarize_search(exports + retrievals)

        assert summary["query_count"] == 100
        assert summary["retrieval_query_count"] == 10
        assert summary["execution_paths"] == {"scope_export": 90, "tree_shadow": 10}
        assert summary["no_entity_match_rate"] == 1.0
        assert summary["resolved_entity_rate"] == 0.0
        # The slow exports no longer set the retrieval latency percentiles.
        assert summary["latency_ms"] == {"p50": 60, "p95": 60, "max": 60, "sample_count": 10}

    def test_known_non_retrieval_paths_are_not_reported_as_unknown(self):
        # These writes are already understood - a bulk export and an
        # anchor-context read from the agent branch, plus the sources entry.
        # If the sentinel fired on them it would never stop firing.
        rows = [
            {"execution_path": path, "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": False, "resolved_entity_count": 0,
             "locate_ms": 0.0, "duration_ms": 40}
            for path in ("scope_export", "context_scope", "metadata_filter")
        ]

        summary = summarize_search(rows)

        assert summary["unknown_path_count"] == 0
        assert summary["unknown_paths"] == []
        assert evaluate_alerts({"search": summary}) == []

    def test_an_unlabelled_writer_is_reported(self):
        # The failure mode this sentinel exists for: another build starts
        # writing rows this service cannot classify. An empty path counts -
        # that is how the anchor-context reads landed in one build.
        rows = [
            {"execution_path": "", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": False, "resolved_entity_count": 0,
             "locate_ms": 0.0, "duration_ms": 30},
            {"execution_path": "context_snapshot", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 0.0, "duration_ms": 30},
            {"execution_path": "tree_shadow", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 1, "locate_ms": 400.0, "duration_ms": 60},
        ]

        summary = summarize_search(rows)

        assert summary["unknown_path_count"] == 2
        assert summary["unknown_paths"] == ["", "context_snapshot"]
        # The known row still counts normally; the unknown ones are excluded
        # from the rates but surfaced as an alert.
        assert summary["retrieval_query_count"] == 1
        assert "unknown_search_path" in evaluate_alerts({"search": summary})

    def test_only_requests_that_ran_location_count_towards_the_rates(self):
        # After the entry split, `traditional` covers two very different rows: one
        # that located and then fell back, and one from a hybrid entry that never
        # located at all. The path label cannot separate them, so the gate is
        # `locate_ms` - otherwise the search box's traffic would dilute the
        # location rate exactly the way the exports used to.
        rows = [
            {"execution_path": "traditional", "fallback_reason": "no_entity_match",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 300.0, "duration_ms": 80},
            {"execution_path": "traditional", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 0.0, "duration_ms": 30},
            # A path this service does not know about (NULL, or a name another
            # build writes) is left out rather than guessed at.
            {"execution_path": "", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": False, "resolved_entity_count": 0,
             "locate_ms": 200.0, "duration_ms": 50},
            {"execution_path": "context_scope", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "locate_ms": 0.0, "duration_ms": 40},
        ]

        summary = summarize_search(rows)

        assert summary["query_count"] == 4
        assert summary["retrieval_query_count"] == 1
        assert summary["no_entity_match_rate"] == 1.0
        assert summary["latency_ms"]["sample_count"] == 1

    def test_locate_percentile_keeps_the_l4_path_out(self):
        rows = [
            {"execution_path": "tree", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": False, "resolved_entity_count": 1,
             "locate_ms": ms, "duration_ms": 40}
            for ms in (10.0, 20.0, 30.0, 40.0)
        ] + [
            {"execution_path": "tree", "fallback_reason": "", "degraded_reason": "",
             "llm_invoked": True, "resolved_entity_count": 1,
             "locate_ms": 4800.0, "duration_ms": 5200},
        ]

        summary = summarize_search(rows)

        # A query that escalated to L4 is counted separately: one LLM round trip
        # would otherwise become the "常规路径" percentile.
        assert summary["locate_latency_ms"] == {
            "p50": 30, "p95": 40, "max": 40, "sample_count": 4,
        }
        assert summary["locate_escalated_ms"] == {
            "p50": 4800, "p95": 4800, "max": 4800, "sample_count": 1,
        }
        # The whole-retrieval percentile still covers every attempt.
        assert summary["latency_ms"]["sample_count"] == 5

    def test_export_only_window_raises_no_alert(self):
        rows = [
            {"execution_path": "scope_export", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "duration_ms": 900}
            for _ in range(50)
        ]

        summary = summarize_search(rows)

        assert summary["retrieval_query_count"] == 0
        assert summary["no_entity_match_rate"] == 0.0
        assert evaluate_alerts({"search": summary}) == []

    def test_sampled_out_shadow_requests_are_not_counted_as_misses(self):
        # Sampling is a budgeting decision. Counting a deliberate skip as "no
        # entity matched" would make the location rate look broken exactly when
        # the sample rate was tuned down.
        rows = [{
            "execution_path": "tree_shadow", "fallback_reason": "",
            "degraded_reason": "", "llm_invoked": False,
            "resolved_entity_count": 0, "locate_ms": 400.0, "duration_ms": 40,
        }] + [
            {"execution_path": "tree_shadow", "fallback_reason": "",
             "degraded_reason": "", "llm_invoked": False,
             "resolved_entity_count": 0, "shadow_sampled": False,
             "duration_ms": 30}
            for _ in range(9)
        ]

        summary = summarize_search(rows)

        assert summary["query_count"] == 10
        assert summary["retrieval_query_count"] == 1
        assert summary["shadow_skipped_count"] == 9
        assert summary["no_entity_match_rate"] == 0.0
        assert summary["latency_ms"] == {
            "p50": 40, "p95": 40, "max": 40, "sample_count": 1,
        }

    def test_high_no_entity_match_is_flagged(self):
        alerts = evaluate_alerts({
            "search": {"query_count": 10, "no_entity_match_rate": 0.7},
        })

        assert "high_no_entity_match" in alerts

    def test_no_queries_means_no_alert(self):
        assert evaluate_alerts({"search": {"query_count": 0, "no_entity_match_rate": 0.0}}) == []

    def test_snapshot_includes_the_search_section(self):
        repo = InMemoryRagMVPRepository()
        repo.record_search(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            execution_path="tree", duration_ms=50,
            diagnostics={
                "resolved_entity_count": 1, "locate_llm_invoked": True,
                "locate_ms": 120.0,
            },
        )

        snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

        assert snapshot["search"]["query_count"] == 1
        assert snapshot["search"]["l4_invocation_rate"] == 1.0
        assert snapshot["search"]["resolved_entity_rate"] == 1.0


class TestReviewMetrics:
    """审核吞吐：批准率与拒绝率从审核记录直接推出，不需要新的埋点表。"""

    def _reviewed_candidates(self):
        repo = InMemoryRagMVPRepository()
        chunk = _chunk(1)
        repo.upsert_chunks([chunk])
        candidate_ids = [
            repo.upsert_candidate_mention(
                scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
                candidate_name=f"{name}", normalized_key=f"{name}",
                domain="project", chunk=chunk, context_excerpt=chunk.content,
                confidence=0.85, method="llm",
            )
            for name in ("A项目", "B项目", "C项目")
        ]
        return repo, candidate_ids

    def _review(self, repo, candidate_id, action, index, **extra):
        repo.review_candidate(
            scope_type=SCOPE["scope_type"],
            scope_id=SCOPE["scope_id"],
            candidate_id=candidate_id,
            reviewer_id="00000000-0000-0000-0000-000000000099",
            review_request_id=f"00000000-0000-0000-0000-00000000010{index}",
            action=action,
            expected_status="new",
            canonical_name=f"实体{index}",
            domain="project",
            **extra,
        )

    def test_defer_is_not_counted_as_a_rejection(self):
        repo, (first, second, third) = self._reviewed_candidates()
        self._review(repo, first, "promote", 1)
        self._review(repo, second, "ignore", 2)
        self._review(repo, third, "defer", 3)

        metrics = repo.tree_metrics(**SCOPE)

        assert metrics["review_actions"] == {"promote": 1, "ignore": 1, "defer": 1}
        assert metrics["review_count"] == 3
        # One approval, one rejection, one "later": 1/2, not 1/3.
        assert metrics["review_approval_rate"] == 0.5

    def test_no_reviews_reports_zero_without_dividing_by_zero(self):
        repo, _ = self._reviewed_candidates()

        metrics = repo.tree_metrics(**SCOPE)

        assert metrics["review_count"] == 0
        assert metrics["review_approval_rate"] == 0.0

    def test_dwell_time_average_ignores_reviews_that_reported_nothing(self):
        repo, (first, second, third) = self._reviewed_candidates()
        self._review(repo, first, "promote", 7, duration_ms=4000)
        self._review(repo, second, "ignore", 8, duration_ms=2000)
        # 批量/脚本审核没有停留过程，写 NULL——不能被当成 0 拉低平均。
        self._review(repo, third, "ignore", 9)

        metrics = repo.tree_metrics(**SCOPE)

        assert metrics["review_duration_avg_ms"] == 3000.0
        assert metrics["review_duration_sample_count"] == 2

    def test_merge_counts_as_an_approval(self):
        repo, (first, second, _) = self._reviewed_candidates()
        target = repo.upsert_entity(
            scope_type=SCOPE["scope_type"],
            scope_id=SCOPE["scope_id"],
            domain="project",
            canonical_name="既有实体",
            normalized_key="既有实体",
        )["id"]
        self._review(repo, first, "merge", 4, target_entity_id=target)
        self._review(repo, second, "ignore", 5)

        metrics = repo.tree_metrics(**SCOPE)

        assert metrics["review_approval_rate"] == 0.5

    def test_the_snapshot_carries_review_metrics(self):
        repo, (first, _, _) = self._reviewed_candidates()
        self._review(repo, first, "promote", 6)

        snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

        assert snapshot["review_count"] == 1
        assert snapshot["review_approval_rate"] == 1.0


class TestScanMetrics:
    def test_empty_history_reports_zeros(self):
        summary = summarize_scan_runs([])

        assert summary["run_count"] == 0
        assert summary["empty_window_ratio"] == 0.0
        assert summary["last_run_at"] is None

    def test_aggregates_sweeps_and_computes_the_empty_ratio(self):
        summary = summarize_scan_runs([
            {"conversations": 2, "windows": 6, "empty_windows": 3, "mounts": 4,
             "candidates": 2, "relations": 1, "failed_conversations": 0,
             "created_at": "2026-10-12T10:00:00Z"},
            {"conversations": 1, "windows": 4, "empty_windows": 1, "mounts": 2,
             "candidates": 1, "relations": 0, "failed_conversations": 1,
             "created_at": "2026-10-12T09:00:00Z"},
        ])

        assert summary["run_count"] == 2
        assert summary["windows"] == 10
        assert summary["empty_windows"] == 4
        assert summary["empty_window_ratio"] == 0.4
        assert summary["mounts"] == 6
        assert summary["failed_conversations"] == 1
        assert summary["last_run_at"] == "2026-10-12T10:00:00Z"

    def test_high_empty_window_rate_is_flagged(self):
        alerts = evaluate_alerts({
            "scan": {"windows": 10, "empty_window_ratio": 0.8},
        })

        assert "high_empty_window_rate" in alerts

    def test_scan_alert_needs_windows(self):
        # An empty ratio without any windows is not a signal.
        assert evaluate_alerts({"scan": {"windows": 0, "empty_window_ratio": 1.0}}) == []

    def test_snapshot_includes_the_scan_section(self):
        repo = InMemoryRagMVPRepository()
        repo.record_scan_run(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            conversations=1, windows=5, empty_windows=1, mounts=7,
        )

        snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

        assert snapshot["scan"]["run_count"] == 1
        assert snapshot["scan"]["empty_window_ratio"] == 0.2
        assert snapshot["scan"]["mounts"] == 7


class TestPrometheusRendering:
    def test_flattens_the_dashboard_numbers(self):
        values = flatten_metrics({
            "entity_count": 4, "message_count": 100, "mounted_chunk_count": 25,
            "mount_count": 30, "mount_coverage": 0.25, "pending_candidate_count": 7,
            "relation_count": 3, "review_count": 5, "review_approval_rate": 0.6,
            "review_duration_avg_ms": 4200.0, "review_duration_sample_count": 4,
            "entities_missing_embedding": 1,
            "alerts": ["no_mounts"],
            "search": {"query_count": 10, "retrieval_query_count": 8,
                       "shadow_skipped_count": 2,
                       "unknown_path_count": 1,
                       "no_entity_match_rate": 0.2,
                       "resolved_entity_rate": 0.8, "l4_invocation_rate": 0.1,
                       "latency_ms": {"p50": 120, "p95": 900},
                       "locate_latency_ms": {"p50": 18, "p95": 42, "sample_count": 7},
                       "locate_escalated_ms": {"p95": 4100, "sample_count": 1}},
            "scan": {"windows": 20, "empty_window_ratio": 0.15, "mounts": 5, "candidates": 9},
        })

        assert values["rag_tree_entity_count"] == 4
        assert values["rag_tree_mount_coverage"] == 0.25
        assert values["rag_tree_alert_count"] == 1
        assert values["rag_tree_review_count"] == 5
        assert values["rag_tree_review_approval_rate"] == 0.6
        assert values["rag_tree_review_duration_avg_seconds"] == 4.2
        assert values["rag_tree_review_duration_sample_count"] == 4
        assert values["rag_tree_search_query_count"] == 10
        assert values["rag_tree_search_retrieval_query_count"] == 8
        assert values["rag_tree_search_shadow_skipped_count"] == 2
        assert values["rag_tree_search_unknown_path_count"] == 1
        assert values["rag_tree_search_latency_p95_ms"] == 900
        assert values["rag_tree_search_locate_p95_ms"] == 42
        assert values["rag_tree_search_locate_sample_count"] == 7
        assert values["rag_tree_search_locate_escalated_p95_ms"] == 4100
        assert values["rag_tree_scan_empty_window_ratio"] == 0.15

    def test_flattening_a_bare_scope_yields_zeros(self):
        values = flatten_metrics({})

        assert values["rag_tree_entity_count"] == 0
        assert values["rag_tree_search_latency_p95_ms"] == 0
        assert values["rag_tree_scan_empty_window_ratio"] == 0.0

    def test_exposition_has_help_type_and_scope_labels(self):
        text = render_prometheus([
            ("organization:org-1", {"entity_count": 2, "mount_coverage": 0.5}),
            ("user:user-1", {"entity_count": 5}),
        ])

        assert "# TYPE rag_tree_entity_count gauge" in text
        assert "# HELP rag_tree_entity_count" in text
        assert 'rag_tree_entity_count{scope="organization:org-1"} 2.0' in text
        assert 'rag_tree_entity_count{scope="user:user-1"} 5.0' in text
        # A scope that never reported a value still gets a zero, so a dashboard
        # does not show a gap where a tenant simply has no mounts yet.
        assert 'rag_tree_mount_coverage{scope="user:user-1"} 0.0' in text

    def test_exposition_of_no_scopes_is_empty(self):
        assert render_prometheus([]) == "\n"

    def test_service_renders_every_active_scope(self):
        repo = InMemoryRagMVPRepository()
        repo.record_scan_run(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            conversations=1, windows=4, empty_windows=1,
        )
        repo.upsert_entity(
            domain="project", canonical_name="A项目",
            normalized_key=normalized_text("A项目"), **SCOPE
        )

        text = TreeMetricsService(repository=repo).prometheus()

        assert f"scope=\"organization:org-1\"" in text
        assert 'rag_tree_entity_count{scope="organization:org-1"} 1.0' in text
