"""The generated report is served only to its owner, inline or as a download."""

from __future__ import annotations

from collections.abc import Callable

import pytest

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeAttachmentStore:
    def __init__(self, records: dict[str, dict]):
        self.records = records

    def metadata(self, attachment_id: str):
        return self.records.get(attachment_id)

    def read(self, attachment_id: str) -> bytes:
        return b"PK-docx-body"


@pytest.fixture
def make_client() -> Callable[[str, dict[str, dict]], object]:
    """Build a client with the store and the caller injected.

    ``app.main`` is imported here rather than at module scope: importing it
    loads ``services/agent/.env`` into the test process, which would change the
    documented config defaults other tests assert against.
    """

    from fastapi.testclient import TestClient

    from app.auth import AuthenticatedUser, current_user
    from app.main import app
    from app.routers import reports

    def build(user_id: str, records: dict[str, dict]):
        store = FakeAttachmentStore(records)
        app.dependency_overrides[reports.get_attachment_store] = lambda: store
        app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
            user_id=user_id, session_id="s1"
        )
        return TestClient(app)

    yield build
    app.dependency_overrides.clear()


RECORDS = {
    "artifact-1": {
        "attachment_id": "artifact-1",
        "owner_id": "u1",
        "file_name": "张三2026-09-28-2026-10-04.docx",
        "mime_type": DOCX,
        "size_bytes": 12,
    }
}


def test_owner_can_preview_and_download_the_report(make_client):
    client = make_client("u1", RECORDS)

    preview = client.get("/api/agent/v1/reports/artifact-1")
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline")
    assert preview.content == b"PK-docx-body"

    download = client.get("/api/agent/v1/reports/artifact-1?download=true")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["content-type"] == DOCX


def test_another_user_cannot_read_the_report(make_client):
    client = make_client("u2", RECORDS)

    response = client.get("/api/agent/v1/reports/artifact-1")

    assert response.status_code == 404


def test_missing_report_is_a_404(make_client):
    client = make_client("u1", RECORDS)

    assert client.get("/api/agent/v1/reports/nope").status_code == 404
