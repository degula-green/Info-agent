"""The deploy files have to parse, and every migration they list must exist.

A compose file is only exercised at deploy time, so a broken block scalar or a
typo in a migration name costs a deployment rather than a test run. Both have
already happened in review: one migration was referenced by nothing at all, and
an edit here silently broke the `knowledge-migrate` block scalar's indentation.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER = REPO_ROOT / "docker"
MIGRATIONS = REPO_ROOT / "db" / "migrations"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.server.yml")

# Knowledge-schema migrations that landed with the agent branch. They must be
# applied by the deploy entry points, not merely committed.
AGENT_KNOWLEDGE_MIGRATIONS = (
    "20261007_contact_name_core.up.sql",
    "20261007_knowledge_lifecycle_ready.up.sql",
    "20261009_wechat_contact_book.up.sql",
)

# docker-compose.yml spells the whole path (/migrations/<file>.sql); the server
# file lists bare file names and builds the path with ${migration}. Matching the
# name itself covers both.
MIGRATION_NAME = re.compile(r"\b(\d{8}_[0-9A-Za-z_.-]+\.sql)\b")


def _compose(name: str) -> dict:
    return yaml.safe_load((DOCKER / name).read_text(encoding="utf-8"))


def _migration_command(data: dict) -> str:
    command = data["services"]["knowledge-migrate"]["command"]
    return " ".join(command) if isinstance(command, list) else str(command)


def test_every_compose_file_parses():
    # A literal block scalar only has to be under-indented once to make the
    # whole file unreadable to the compose parser.
    for name in COMPOSE_FILES:
        with_subtest = _compose(name)
        assert with_subtest.get("services"), name


def test_referenced_migrations_exist_on_disk():
    for name in COMPOSE_FILES:
        referenced = re.findall(
            MIGRATION_NAME, _migration_command(_compose(name))
        )
        assert referenced, f"{name} 没有引用任何迁移"

        missing = [item for item in referenced if not (MIGRATIONS / item).exists()]
        assert not missing, f"{name} 引用了不存在的迁移: {missing}"


def test_the_knowledge_migrate_chain_runs_the_agent_knowledge_migrations():
    # knowledge_lifecycle_ready widens knowledge_items_lifecycle_chk to accept
    # 'ready'. It was committed but wired into nothing, so a fresh deploy would
    # reject the local-upload ready write.
    for name in COMPOSE_FILES:
        command = _migration_command(_compose(name))
        missing = [item for item in AGENT_KNOWLEDGE_MIGRATIONS if item not in command]
        assert not missing, f"{name} 缺少 agent 侧 knowledge 迁移: {missing}"


def test_the_dev_compose_mounts_every_migration_it_runs():
    # docker-compose.yml mounts files one by one (the server file mounts the
    # whole directory), so a listed-but-unmounted migration fails at runtime.
    data = _compose("docker-compose.yml")
    volumes = " ".join(data["services"]["knowledge-migrate"]["volumes"])

    referenced = MIGRATION_NAME.findall(_migration_command(data))
    not_mounted = [item for item in referenced if item not in volumes]
    assert not not_mounted, f"docker-compose.yml 未挂载却要执行的迁移: {not_mounted}"
