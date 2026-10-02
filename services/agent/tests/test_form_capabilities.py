"""form.preview / form.apply against a fake browser client."""

from __future__ import annotations

import pytest

from app.capabilities.form import (
    APPLY_NAME,
    PREVIEW_NAME,
    FormApplyCapability,
    FormPreviewCapability,
    extract_values,
    fingerprint_headers,
)
from app.infrastructure.web.form_browser_client import FormBrowserError

URL = "https://www.kdocs.cn/l/cvXKFoIH3ySI"
HEADERS = ["学号", "姓名", "性别", "联系电话"]


class FakeClient:
    def __init__(self, *, page=None, grid=None, written=None) -> None:
        self.page = page or {
            "final_url": URL,
            "title": "班级通讯录",
            "kind": "spreadsheet",
            "login_required": False,
        }
        self.grid = grid or {"headers": HEADERS, "rows": [["1", "甲", "男", "1"], ["2", "乙", "女", "2"]]}
        self.written = written or {
            "written_range": "A4:D4",
            "observed": [["1000023", "测试甲", "男", "13900000000"]],
            "verified": True,
        }
        self.closed: list[str] = []
        self.writes: list[tuple[str, str, list]] = []

    def create_session(self) -> str:
        return "sess-1"

    def open(self, session_id: str, url: str) -> dict:
        return self.page

    def read_grid(self, session_id: str) -> dict:
        return self.grid

    def write_grid(self, session_id: str, start_cell: str, values: list) -> dict:
        self.writes.append((session_id, start_cell, values))
        return self.written

    def clear_range(self, session_id: str, span: str) -> None:
        return None

    def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)


def test_extract_values_prefers_the_longest_header() -> None:
    text = "填写 家长姓名：王家长 姓名：小王"
    found = extract_values(["姓名", "家长姓名"], text)
    assert found == {"家长姓名": "王家长", "姓名": "小王"}


def test_extract_values_stops_at_the_next_header() -> None:
    text = "姓名：张三 性别：男 联系电话：13800000000"
    assert extract_values(["姓名", "性别", "联系电话"], text) == {
        "姓名": "张三",
        "性别": "男",
        "联系电话": "13800000000",
    }


def test_fingerprint_is_stable_and_layout_sensitive() -> None:
    assert fingerprint_headers(HEADERS) == fingerprint_headers(list(HEADERS))
    assert fingerprint_headers(HEADERS) != fingerprint_headers(["姓名", "学号"])


def test_preview_builds_a_draft_and_targets_the_next_empty_row() -> None:
    client = FakeClient()
    capability = FormPreviewCapability(client)
    request = f"帮我填写这个表格 {URL} 学号：1000023 姓名：测试甲 性别：男 联系电话：13900000000"
    result = capability.execute(capability.validate({"request": request, "url": URL}))
    draft = result["form"]
    assert draft["target_cell"] == "A4"
    assert draft["write_model"] == "live_document"
    assert draft["page_fingerprint"] == fingerprint_headers(HEADERS)
    assert [f["name"] for f in draft["fields"]] == HEADERS
    assert draft["fields"][1]["value"] == "测试甲"
    assert draft["missing"] == []
    # The preview must not leave a browser session behind.
    assert client.closed == ["sess-1"]


def test_preview_reports_missing_fields() -> None:
    client = FakeClient()
    capability = FormPreviewCapability(client)
    request = f"帮我填写这个表格 {URL} 姓名：只有姓名"
    result = capability.execute(capability.validate({"request": request, "url": URL}))
    assert result["form"]["missing"] == ["学号", "性别", "联系电话"]
    assert result["warnings"]


def test_preview_pauses_for_takeover_when_login_is_required() -> None:
    client = FakeClient(
        page={"final_url": URL, "title": "班级通讯录", "kind": "spreadsheet", "login_required": True}
    )
    capability = FormPreviewCapability(client)
    result = capability.execute(
        capability.validate({"request": f"填写 {URL}", "url": URL})
    )
    assert result["requires_user_input"] is True
    assert "form_login" in result["missing_information"]


def test_preview_refuses_a_link_the_owner_did_not_write() -> None:
    capability = FormPreviewCapability(FakeClient())
    with pytest.raises(FormBrowserError):
        capability.execute(
            capability.validate({"request": "帮我填一下", "url": URL})
        )


def _draft(client: FakeClient) -> dict:
    capability = FormPreviewCapability(client)
    request = f"填写 {URL} 学号：1000023 姓名：测试甲 性别：男 联系电话：13900000000"
    return capability.execute(capability.validate({"request": request, "url": URL}))["form"]


def test_apply_writes_and_reports_verification() -> None:
    client = FakeClient()
    draft = _draft(client)
    capability = FormApplyCapability(client)
    result = capability.execute(
        capability.validate(
            {
                "request": f"填写 {URL}",
                "draft": draft,
                "action": "write_cells",
                "values": [["1000023", "测试甲", "男", "13900000000"]],
                "idempotency_key": "task-1",
            }
        )
    )
    assert result["operation"] == APPLY_NAME
    assert result["verified"] is True
    assert result["written_range"] == "A4:D4"
    assert client.writes and client.writes[0][1] == "A4"


def test_apply_refuses_when_the_layout_changed() -> None:
    client = FakeClient()
    draft = _draft(client)
    client.grid = {"headers": ["姓名", "学号"], "rows": []}
    capability = FormApplyCapability(client)
    with pytest.raises(FormBrowserError):
        capability.execute(
            capability.validate(
                {
                    "request": f"填写 {URL}",
                    "draft": draft,
                    "action": "write_cells",
                    "values": [["测试甲", "1000023"]],
                    "idempotency_key": "task-1",
                }
            )
        )
    assert client.writes == []


def test_apply_pauses_when_login_is_required() -> None:
    client = FakeClient()
    draft = _draft(client)
    client.page = {"final_url": URL, "title": "班级通讯录", "kind": "spreadsheet", "login_required": True}
    capability = FormApplyCapability(client)
    result = capability.execute(
        capability.validate(
            {
                "request": f"填写 {URL}",
                "draft": draft,
                "action": "write_cells",
                "values": [["1000023", "测试甲", "男", "13900000000"]],
                "idempotency_key": "task-1",
            }
        )
    )
    assert result["requires_user_input"] is True


def test_descriptors_declare_the_right_risk() -> None:
    preview = FormPreviewCapability(FakeClient()).descriptor
    apply_cap = FormApplyCapability(FakeClient()).descriptor
    assert preview.name == PREVIEW_NAME
    assert preview.side_effect is False and preview.requires_approval is False
    assert apply_cap.name == APPLY_NAME
    assert apply_cap.side_effect is True and apply_cap.requires_approval is True


def test_apply_binds_the_draft_to_the_preview_step() -> None:
    """The planner points at the preview; it must not retype the draft."""

    descriptor = FormApplyCapability(FakeClient()).descriptor
    binding = descriptor.input_bindings[0]
    assert binding.planner_argument == "draft_ref"
    assert binding.runtime_argument == "draft"
    assert binding.source_capability == PREVIEW_NAME
    assert binding.source_output == "form"
    # The runtime argument is replaced by the reference in what the model sees.
    assert "draft" not in descriptor.planner_input_schema.get("properties", {})


def test_prompt_arguments_only_expose_action() -> None:
    from app.planning.schema import planner_arguments_schema

    schema = planner_arguments_schema(FormApplyCapability(FakeClient()).descriptor)
    assert set(schema["properties"]) == {"action", "draft_ref"}
    assert "draft_ref" in schema["required"]
    assert "request" not in schema["properties"]


def test_apply_derives_values_from_the_confirmed_draft() -> None:
    client = FakeClient()
    preview = FormPreviewCapability(client)
    request = f"填写 {URL} 学号：1000023 姓名：测试甲"
    draft = preview.execute(preview.validate({"request": request, "url": URL}))["form"]
    assert draft["missing"] == ["性别", "联系电话"]
    # Fill the rest the way the owner would on the review card.
    for field in draft["fields"]:
        if not field["value"]:
            field["value"] = "x"
    capability = FormApplyCapability(client)
    capability.execute(
        capability.validate(
            {
                "request": f"填写 {URL}",
                "draft": draft,
                "action": "write_cells",
                "idempotency_key": "task-1",
            }
        )
    )
    assert client.writes[0][2] == [["1000023", "测试甲", "x", "x"]]


def test_live_document_overrides_a_planner_submit_action() -> None:
    """A spreadsheet has no submit control, so the draft decides the action."""

    client = FakeClient()
    draft = _draft(client)
    capability = FormApplyCapability(client)
    result = capability.execute(
        capability.validate(
            {
                "request": f"填写 {URL}",
                "draft": draft,
                "action": "fill_and_submit",
                "idempotency_key": "task-1",
            }
        )
    )
    assert result["action"] == "write_cells"
