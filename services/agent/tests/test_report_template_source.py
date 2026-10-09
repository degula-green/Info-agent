from app.application.report.template_source import (
    dedupe,
    resolve_template,
    search_terms,
    upload_candidates,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeKnowledge:
    def __init__(self, by_term: dict[str, list[dict]]):
        self.by_term = by_term
        self.calls: list[str] = []

    def search_attachments(self, *, owner_user_id, name, limit=50, request_id="", trace_id=""):
        self.calls.append(name)
        return {"items": self.by_term.get(name, [])}


def _uploaded(name: str = "周报模板.docx", attachment_id: str = "up-1") -> dict:
    return {
        "attachment_id": attachment_id,
        "file_name": name,
        "mime_type": DOCX,
        "size_bytes": 11979,
        "uploaded_at": "2026-10-07T05:00:00+00:00",
        "minio_object_name": f"u/{attachment_id}.docx",
    }


def _collected(attachment_id: str, name: str = "周报模板.docx", digest: str = "abc") -> dict:
    return {
        "attachment_id": attachment_id,
        "file_name": name,
        "mime_type": DOCX,
        "size_bytes": 11979,
        "content_hash": digest,
        "created_at": "2026-10-07T04:00:00+00:00",
    }


def test_search_terms_put_the_exact_name_first():
    assert search_terms("按“周报模板优化版”给我写周报") == [
        "周报模板优化版",
        "周报模板",
        "周报",
        "模板",
    ]


def test_uploaded_marker_uses_only_the_attachment():
    knowledge = FakeKnowledge({"周报模板": [_collected("c1")]})
    result = resolve_template(
        instruction="按我上传的模板给张三写周报",
        uploaded=[_uploaded()],
        knowledge=knowledge,
        owner_user_id="u1",
    )

    assert result.status == "resolved"
    assert result.chosen.origin == "uploaded"
    assert knowledge.calls == []


def test_default_looks_in_collected_documents_first():
    knowledge = FakeKnowledge({"周报模板": [_collected("c1")]})
    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[_uploaded()],
        knowledge=knowledge,
        owner_user_id="u1",
    )

    assert result.status == "resolved"
    assert result.chosen.origin == "collected"
    assert result.chosen.attachment_id == "c1"


def test_attached_template_is_used_when_nothing_is_collected():
    knowledge = FakeKnowledge({})
    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[_uploaded()],
        knowledge=knowledge,
        owner_user_id="u1",
    )

    assert result.status == "resolved"
    assert result.chosen.origin == "uploaded"


def test_missing_template_reports_not_found():
    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[],
        knowledge=FakeKnowledge({}),
        owner_user_id="u1",
    )

    assert result.status == "not_found"
    assert result.candidates == []


def test_identical_copies_collapse_but_distinct_templates_need_a_choice():
    copies = [
        _collected("c1", digest="same"),
        _collected("c2", digest="same"),
        _collected("c3", digest="other"),
    ]
    knowledge = FakeKnowledge({"周报模板": copies})

    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[],
        knowledge=knowledge,
        owner_user_id="u1",
    )

    assert result.status == "needs_selection"
    assert [item.attachment_id for item in result.candidates] == ["c1", "c3"]


def test_renamed_forwarded_copies_collapse_by_content_hash():
    knowledge = FakeKnowledge(
        {
            "周报模板": [
                _collected("c1", name="周报模板.docx", digest="same"),
                _collected("c2", name="周报模板(1).docx", digest="same"),
                _collected("c3", name="周报模板(2).docx", digest="same"),
            ]
        }
    )

    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[],
        knowledge=knowledge,
        owner_user_id="u1",
    )

    assert result.status == "resolved"
    assert result.chosen.attachment_id == "c1"


def test_content_fallback_runs_only_after_the_name_search_misses():
    calls: list[str] = []

    def content_candidates():
        calls.append("content")
        return [_collected("by-content")]

    knowledge = FakeKnowledge({})
    result = resolve_template(
        instruction="给张三写一份周报",
        uploaded=[],
        knowledge=knowledge,
        owner_user_id="u1",
        content_candidates=content_candidates,
    )

    assert calls == ["content"]
    assert result.status == "resolved"
    assert result.chosen.attachment_id == "by-content"


def test_upload_candidates_skip_non_docx_and_missing_ids():
    candidates = upload_candidates(
        [
            {"attachment_id": "a", "file_name": "x.pdf", "mime_type": "application/pdf"},
            {"attachment_id": "", "file_name": "y.docx", "mime_type": DOCX},
            {"attachment_id": "b", "file_name": "z.docx", "mime_type": DOCX},
        ]
    )

    assert [item.attachment_id for item in candidates] == ["b"]


def test_dedupe_keeps_the_first_candidate():
    first = _collected("c1", digest="same")
    second = _collected("c2", digest="same")
    candidates = upload_candidates([_uploaded(attachment_id="u1"), _uploaded(attachment_id="u2", name="x.docx")])

    assert len(candidates) == 2
    assert dedupe(candidates)[0].attachment_id == "u1"
    assert first["content_hash"] == second["content_hash"]
