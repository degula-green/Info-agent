"""Window scan: windowing, entity/relation validation and watermark advance."""

import threading
import time

from app.application.window_scan_service import (
    EntityWindowScanWorker,
    build_extraction_prompt,
    build_windows,
    clean_entities,
    clean_relations,
)
from app.domain.rag import Chunk, normalized_text
from app.infrastructure.extraction.client import ExtractionError
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")
CONVERSATION = "conv-1"


def _chunk(index: int, content: str, sent_at: str) -> Chunk:
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
        content=content,
        content_hash="0" * 64,
        source_conversation_id=CONVERSATION,
        sent_at=sent_at,
        context_header={"sender_display_name": f"用户{index}"},
    )


class FakeExtractor:
    def __init__(self, payload=None, fail=False):
        self.payload = payload or {"entities": [], "relations": []}
        self.fail = fail
        self.prompts = []

    def extract(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise ExtractionError("boom", retryable=True)
        return self.payload


class SlowExtractor:
    """Records how many windows were in flight at once."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.prompts: list[str] = []
        self.lock = threading.Lock()

    def extract(self, prompt):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.prompts.append(prompt)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return {"entities": [], "relations": []}


def _repository_with_messages(count: int, mention: str = "") -> InMemoryRagMVPRepository:
    repo = InMemoryRagMVPRepository()
    chunks = [
        _chunk(i, f"第{i}条消息{mention}", f"2026-10-0{(i % 9) + 1}T0{i % 10}:00:00Z")
        for i in range(1, count + 1)
    ]
    # Keep the timestamps strictly increasing so window ordering is stable.
    for index, chunk in enumerate(chunks, start=1):
        chunk.sent_at = f"2026-10-01T00:{index:02d}:00Z"
    repo.upsert_chunks(chunks)
    return repo


def test_windows_overlap_and_cover_the_tail():
    chunks = [object() for _ in range(45)]

    windows = build_windows(chunks, size=20, step=10)

    assert [len(window) for window in windows] == [20, 20, 20, 15]
    # The final window must include the last message: a plain range() would
    # stop at 40 and leave 41-45 unscanned.
    assert windows[-1][-1] is chunks[-1]
    assert windows[1][0] is chunks[10]  # 50% overlap


def test_short_conversations_become_one_window():
    chunks = [object() for _ in range(5)]
    assert len(build_windows(chunks, size=20, step=10)) == 1
    assert build_windows([], size=20, step=10) == []


def test_clean_entities_drops_unknown_types_and_credentials():
    cleaned = clean_entities([
        {"name": "A项目", "type": "project", "confidence": 0.9},
        {"name": "116服务器", "type": "project", "confidence": 0.6},
        {"name": "某设备", "type": "asset", "confidence": 0.9},
        {"name": "数据库账号root 密码123456", "type": "project", "confidence": 0.5},
        {"name": "张三", "type": "person", "confidence": 0.9, "evidence": "密码是 abc"},
        {"name": "A项目", "type": "project", "confidence": 0.7},
    ])

    assert [item["name"] for item in cleaned] == ["A项目", "116服务器"]
    assert cleaned[0]["confidence"] == 0.9


def test_clean_relations_enforces_the_enum():
    cleaned = clean_relations([
        {"source": "张三", "target": "A项目", "type": "participates_in", "confidence": 0.9},
        {"source": "张三", "target": "A项目", "type": "discusses", "confidence": 0.9},
        {"source": "A项目", "target": "A项目", "type": "related_to", "confidence": 0.9},
        {"source": "", "target": "A项目", "type": "related_to", "confidence": 0.9},
    ])

    assert [item["type"] for item in cleaned] == ["participates_in"]


def test_known_entity_gets_a_window_mount_and_unknown_one_becomes_a_candidate():
    repo = _repository_with_messages(6)
    entity_id = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]
    extractor = FakeExtractor({"entities": [
        {"name": "A项目", "type": "project", "confidence": 0.9},
        {"name": "B项目", "type": "project", "confidence": 0.9},
    ], "relations": []})

    outcome = EntityWindowScanWorker(repository=repo, extractor=extractor).run_once()

    assert outcome.windows == 1
    mounts = [value for value in repo.branches.values() if value["entity_id"] == entity_id]
    assert mounts and all(value["mount_method"] == "window_batch" for value in mounts)
    assert all(value["confidence"] == 0.85 for value in mounts)  # window_strong
    assert outcome.candidates == 1
    assert [item["candidate_name"] for item in repo.candidates.values()] == ["B项目"]


def test_model_failure_leaves_the_watermark_untouched():
    repo = _repository_with_messages(4)
    worker = EntityWindowScanWorker(repository=repo, extractor=FakeExtractor(fail=True))

    outcome = worker.run_once()

    assert outcome.failed_conversations == 1
    assert outcome.windows == 0
    assert repo.get_scan_watermark(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"], conversation_id=CONVERSATION
    ) is None
    # The conversation stays eligible for the next sweep.
    assert repo.list_scan_conversations(limit=5)


def test_successful_scan_advances_the_watermark_and_is_not_rescanned():
    repo = _repository_with_messages(4)
    extractor = FakeExtractor()
    worker = EntityWindowScanWorker(repository=repo, extractor=extractor)

    first = worker.run_once()
    second = worker.run_once()

    assert first.windows == 1
    assert second.windows == 0
    assert repo.get_scan_watermark(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"], conversation_id=CONVERSATION
    ) == "2026-10-01T00:04:00Z"


def test_relations_need_both_endpoints_to_resolve():
    repo = _repository_with_messages(3)
    for name, domain in (("张三", "person"), ("A项目", "project")):
        repo.upsert_entity(
            domain=domain, canonical_name=name, normalized_key=normalized_text(name), **SCOPE
        )
    extractor = FakeExtractor({"entities": [], "relations": [
        {"source": "张三", "target": "A项目", "type": "participates_in", "confidence": 0.9},
        {"source": "张三", "target": "未登记实体", "type": "related_to", "confidence": 0.9},
    ]})

    outcome = EntityWindowScanWorker(repository=repo, extractor=extractor).run_once()

    assert outcome.relations == 1
    assert len(repo.relations) == 1
    relation = next(iter(repo.relations.values()))
    assert relation["relation_type"] == "participates_in"
    assert len(relation["evidence_chunk_ids"]) == 3


def test_relation_confidence_only_moves_up_across_windows():
    repo = _repository_with_messages(3)
    for name, domain in (("张三", "person"), ("A项目", "project")):
        repo.upsert_entity(
            domain=domain, canonical_name=name, normalized_key=normalized_text(name), **SCOPE
        )
    low = FakeExtractor({"entities": [], "relations": [
        {"source": "张三", "target": "A项目", "type": "participates_in", "confidence": 0.7},
    ]})
    EntityWindowScanWorker(repository=repo, extractor=low).run_once()
    high = FakeExtractor({"entities": [], "relations": [
        {"source": "张三", "target": "A项目", "type": "participates_in", "confidence": 0.95},
    ]})
    # Force a re-scan of the same messages by clearing the watermark.
    repo.scan_watermarks.clear()
    EntityWindowScanWorker(repository=repo, extractor=high).run_once()

    assert next(iter(repo.relations.values()))["confidence"] == 0.95


def test_explicit_mentions_are_written_even_when_the_model_returns_nothing():
    repo = _repository_with_messages(3, mention=" 关于A项目")
    entity_id = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]

    EntityWindowScanWorker(repository=repo, extractor=FakeExtractor()).run_once()

    explicit = [
        value for value in repo.branches.values()
        if value["entity_id"] == entity_id and value["mount_method"] == "explicit"
    ]
    assert explicit
    # exact name matches score 1.0 and aliases 0.95; both sit in the explicit tier
    assert all(value["confidence"] >= 0.95 for value in explicit)


def test_prompt_lists_known_entities_so_the_model_can_reuse_their_ids():
    repo = _repository_with_messages(2)
    entity_id = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]
    entities, aliases, _ = repo.load_entity_registry(**SCOPE)
    chunks = repo.list_conversation_chunks(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"], conversation_id=CONVERSATION
    )

    prompt = build_extraction_prompt(chunks, entities, aliases)

    assert entity_id in prompt
    assert "A项目" in prompt
    assert "works_for" in prompt and "participates_in" in prompt


def test_prompt_states_the_known_list_is_reference_only():
    # Measured: with the original wording, a known-entity list of >=10 entries
    # made the model return nothing at all (4 runs: 3,0,0,0 entities). The list
    # framed the task as matching, so an unmatched window produced an empty
    # result. Restating it as reference-only restored 12/12 runs, including at
    # the production limit of 40 entries.
    repo = _repository_with_messages(2)
    repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )
    entities, aliases, _ = repo.load_entity_registry(**SCOPE)
    chunks = repo.list_conversation_chunks(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"], conversation_id=CONVERSATION
    )

    prompt = build_extraction_prompt(chunks, entities, aliases)

    assert "只用于填写 existing_entity_id" in prompt
    assert "不在表里的实体" in prompt


def test_windows_run_concurrently_and_totals_still_add_up():
    # 45 messages at size 20 / step 10 gives 4 overlapping windows.
    repo = _repository_with_messages(45)
    extractor = SlowExtractor()
    worker = EntityWindowScanWorker(repository=repo, extractor=extractor, concurrency=4)

    outcome = worker.run_once(conversation_limit=1)

    assert outcome.windows == 4
    assert outcome.failed_conversations == 0
    # Serial execution would never exceed 1 window in flight.
    assert extractor.max_active > 1
    # Every window still produced its prompt, and the watermark advanced once.
    assert len(extractor.prompts) == 4
    assert repo.get_scan_watermark(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"], conversation_id=CONVERSATION
    ) == "2026-10-01T00:45:00Z"
