"""Configuration tests: .env style variables and process-environment precedence."""

from __future__ import annotations

import importlib

import app.config as config_module


def _reload_with(monkeypatch, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


def test_uppercase_prefix_wins_and_lowercase_prefix_is_supported(monkeypatch) -> None:
    module = _reload_with(
        monkeypatch,
        agent_DATABASE_URL="postgresql://lower/db",
        AGENT_DATABASE_URL="postgresql://upper/db",
        agent_REDIS_URL="redis://lower:6379/0",
    )
    try:
        assert module.settings.database_url == "postgresql://upper/db"
        assert module.settings.redis_url == "redis://lower:6379/0"
        assert module.settings.database_schema == "agent"
    finally:
        for key in ("agent_DATABASE_URL", "AGENT_DATABASE_URL", "agent_REDIS_URL"):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(config_module)


def test_documented_defaults_and_numeric_parsing(monkeypatch) -> None:
    module = _reload_with(
        monkeypatch,
        AGENT_TASK_MAX_STEPS="3",
        AGENT_TASK_MAX_EXECUTION_SECONDS="12.5",
        AGENT_TASK_RETRY_BACKOFF_SECONDS="0,2",
    )
    try:
        assert module.settings.task_max_steps == 3
        assert module.settings.task_max_execution_seconds == 12.5
        assert module.settings.retry_backoff_seconds == [0.0, 2.0]
        assert module.settings.redis_inbound_stream == "agent:tasks"
    finally:
        for key in (
            "AGENT_TASK_MAX_STEPS",
            "AGENT_TASK_MAX_EXECUTION_SECONDS",
            "AGENT_TASK_RETRY_BACKOFF_SECONDS",
        ):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(config_module)


def test_missing_database_url_is_detectable(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("agent_DATABASE_URL", raising=False)
    module = importlib.reload(config_module)
    try:
        assert module.settings.database_url == ""
    finally:
        importlib.reload(config_module)


def test_invalid_integer_raises(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_TASK_MAX_STEPS", "not-a-number")
    import pytest

    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config_module)
    finally:
        monkeypatch.delenv("AGENT_TASK_MAX_STEPS", raising=False)
        importlib.reload(config_module)


def test_knowledge_platform_allowlist_defaults_and_overrides(monkeypatch) -> None:
    module = importlib.reload(config_module)
    assert module.settings.knowledge_platform_allowlist == ("feishu", "wecom", "wechat")
    assert module.settings.knowledge_base_url == ""

    module = _reload_with(monkeypatch, AGENT_KNOWLEDGE_PLATFORMS=" Feishu , wechat ")
    try:
        assert module.settings.knowledge_platform_allowlist == ("feishu", "wechat")
    finally:
        monkeypatch.delenv("AGENT_KNOWLEDGE_PLATFORMS", raising=False)
        importlib.reload(config_module)


def test_empty_knowledge_platform_override_falls_back_to_defaults(monkeypatch) -> None:
    module = _reload_with(monkeypatch, AGENT_KNOWLEDGE_PLATFORMS=" , ")
    try:
        assert module.settings.knowledge_platform_allowlist == ("feishu", "wecom", "wechat")
    finally:
        monkeypatch.delenv("AGENT_KNOWLEDGE_PLATFORMS", raising=False)
        importlib.reload(config_module)
