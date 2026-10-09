from __future__ import annotations

from io import BytesIO

from docx import Document

from app.application.report.template import parse_template


def _template_bytes() -> bytes:
    document = Document()
    for line in (
        "## 周报模板（通用版）",
        "**汇报人：** [你的姓名]",
        "**周期：** 2026年9月28日 — 2026年10月4日",
        "### 一、本周工作完成情况",
        "| 序号 | 工作事项 | 完成情况 | 关键产出/数据 | 状态 |",
        "|---|---|---|---|---|",
        "| 1 | [事项名称] | [已完成/进行中/延期] | [具体结果] | ✅/🔄/⚠️ |",
        "### 二、下周工作计划",
        "| 优先级 | 工作事项 | 预期目标 | 计划完成时间 | 所需支持 |",
        "|---|---|---|---|---|",
        "| P0 | [最重要的事] | [可量化目标] | [日期] | [资源/协作] |",
        "### 三、思考与建议（可选）",
        "- **流程优化：** [发现的问题 + 建议]",
        "### 四、需领导决策/协调事项",
        "1. [具体事项，说明背景与选项]",
    ):
        document.add_paragraph(line)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_parse_template_collects_slots() -> None:
    spec = parse_template(_template_bytes())
    assert spec.title == "周报模板（通用版）"
    slots = spec.slot_map()
    assert "title" in slots
    assert slots["field.汇报人"].label == "汇报人"
    # The period line has no placeholder but must still be fillable.
    assert "field.周期" in slots
    work = slots["table.一_本周工作完成情况.0"]
    assert work.kind == "table_row"
    assert work.columns == ["序号", "工作事项", "完成情况", "关键产出/数据", "状态"]
    assert "table.二_下周工作计划.0" in slots
    assert "bullet.三_思考与建议_可选.0" in slots
    assert "number.四_需领导决策_协调事项.0" in slots
