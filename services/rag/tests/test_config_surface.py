"""Every declared setting must either be read or be declared reserved.

`RAG_METRICS_ENABLED`, `RAG_TREE_SHADOW_SAMPLE_RATE` and
`RAG_MEMORY_REGEX_CANDIDATES_ENABLED` were all declared, documented in .env, and
read by nobody - the operator's knob did nothing and the failure was silent
(the settings looked wired). This test makes that state explicit: a setting is
either referenced as `settings.<name>` somewhere in the service, or it is listed
below with the reason it is allowed to sit unused.

The list is checked for staleness too - wiring a reserved setting up without
removing it here fails, so the list cannot quietly drift away from reality.
"""

import re
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG = SERVICE_ROOT / "app" / "config.py"

# Declared at class level, so four spaces of indentation.
_DECLARATION = re.compile(
    r"^\s{4}([a-z][a-z0-9_]*)\s*:\s*[^=]+=", re.MULTILINE
)
# Python keywords that the declaration regex can still trip over (`try:` blocks
# inside methods that contain an `=` later on the line).
_NOT_SETTINGS = {"try"}

# Settings with no reader today. Grouped by why they are allowed to stay: the
# point is that each one is a decision, not an oversight. Delete an entry when
# the feature lands, and delete the setting instead if the decision is "never".
RESERVED_WITHOUT_READERS = {
    # --- 部署形态提供的参数：由启动命令/编排决定，应用自身不读 ---------------
    "http_host",                  # uvicorn --host 由启动脚本传入
    "http_port",                  # uvicorn --port 由启动命令传入
    # --- 超时由调用方按次传入，未接到配置 -----------------------------------
    "elasticsearch_connect_timeout_seconds",
    "embedding_connect_timeout_seconds",
    "authz_connect_timeout_seconds",
    "knowledge_connect_timeout_seconds",
    "redis_connect_timeout_seconds",
    "qa_connect_timeout_seconds",
    # --- 重试当前写死在实现里 ----------------------------------------------
    "embedding_max_retries",
    "extract_max_retries",
    # --- 规划中的能力：配置先留，代码还没接 ---------------------------------
    "rerank_enabled",             # 计划里的可选 rerank，尚未实现
    "rerank_api_base_url",
    "rerank_api_key",
    "rerank_model",
    "rerank_timeout_seconds",
    "rerank_top_n",
    "query_rewrite_enabled",      # 查询改写属于设计里的候选优化
    "query_rewrite_max",
    "otel_enabled",               # 计划 4.2.3 的可观测性，当前只有 /metrics
    "otel_exporter_otlp_endpoint",
    # --- 遗留旋钮：既不生效也没有明确规划，待产品/运维确认后删除 -------------
    "authz_caller_service",
    "authz_scope_max_objects",
    "redis_dlq_stream",
    "minio_presigned_url_ttl_seconds",
    "artifact_retention_days",
    "task_visibility_timeout_seconds",
    "index_bulk_size",
    "index_flush_interval_ms",
    "worker_concurrency",
    "search_deadline_ms",
    "final_top_k",
    "highlight_final_only",
}


def _declared_settings() -> list[str]:
    names = _DECLARATION.findall(CONFIG.read_text(encoding="utf-8"))
    return [name for name in names if name not in _NOT_SETTINGS]


def _source_texts() -> list[str]:
    return [
        path.read_text(encoding="utf-8", errors="ignore")
        for path in SERVICE_ROOT.rglob("*.py")
        if ".venv" not in path.parts and path.name != "config.py"
    ]


def _settings_without_readers() -> set[str]:
    texts = _source_texts()
    return {
        name
        for name in _declared_settings()
        if not any(re.search(rf"\bsettings\.{name}\b", text) for text in texts)
    }


def test_the_allowlist_matches_reality_exactly():
    unread = _settings_without_readers()

    newly_dead = unread - RESERVED_WITHOUT_READERS
    assert not newly_dead, (
        "这些设置声明了但没有人读，也没有登记为保留项："
        f"{sorted(newly_dead)}。要么接上代码，要么删掉，要么登记进 "
        "RESERVED_WITHOUT_READERS 并写清原因。"
    )

    stale = RESERVED_WITHOUT_READERS - unread
    assert not stale, (
        "这些设置已经有读取方（或已被删除），应从 RESERVED_WITHOUT_READERS 移除："
        f"{sorted(stale)}"
    )


def test_the_scanner_actually_finds_settings():
    # A guard on the guard: if config.py moves or the declaration shape changes,
    # the regex above would silently match nothing and both tests would pass.
    declared = _declared_settings()

    assert len(declared) > 150, f"只解析出 {len(declared)} 个设置，解析规则可能过期"
    assert "tree_mode" in declared
    assert "rag_internal_token" in declared


def test_settings_that_were_wired_up_are_not_still_listed():
    # Spot-check the three dead knobs this suite was written for: they must be
    # read now, which also means they must not appear in the allowlist.
    wired = {"metrics_enabled", "tree_shadow_sample_rate", "memory_regex_candidates_enabled"}

    assert wired & RESERVED_WITHOUT_READERS == set()
    assert wired & _settings_without_readers() == set()
