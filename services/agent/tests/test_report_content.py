from datetime import datetime
from zoneinfo import ZoneInfo

from app.application.report.content import (
    EMPTY_SECTION,
    NOT_INFERRED,
    build_slot_values,
    clean_text,
)
from app.application.report.period import previous_week
from app.application.report.template import TemplateSlot, TemplateSpec

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _window():
    return previous_week(
        datetime(2026, 10, 7, 15, 30, tzinfo=SHANGHAI), "Asia/Shanghai"
    )


def _spec() -> TemplateSpec:
    return TemplateSpec(
        title="周报模板（通用版）",
        paragraphs=[],
        slots=[
            TemplateSlot(
                key="title",
                kind="title",
                paragraph_index=0,
                section="",
                template_text="## 周报模板（通用版）",
            ),
            TemplateSlot(
                key="field.汇报人",
                kind="field",
                paragraph_index=1,
                section="",
                label="汇报人",
                template_text="**汇报人：** [你的姓名]",
            ),
            TemplateSlot(
                key="field.周期",
                kind="field",
                paragraph_index=2,
                section="",
                label="周期",
                template_text="**周期：** 2026年9月28日",
            ),
            TemplateSlot(
                key="field.部门_项目",
                kind="field",
                paragraph_index=3,
                section="",
                label="部门/项目",
                template_text="**部门/项目：** [部门或项目名称]",
            ),
            TemplateSlot(
                key="table.work.0",
                kind="table_row",
                paragraph_index=4,
                section="一、本周工作完成情况",
                columns=["序号", "工作事项", "完成情况", "关键产出/数据", "状态"],
                template_text="| 1 | [事项名称] | [已完成/进行中/延期] | [具体结果] | ✅/🔄/⚠️ |",
            ),
            TemplateSlot(
                key="table.work.1",
                kind="table_row",
                paragraph_index=5,
                section="一、本周工作完成情况",
                columns=["序号", "工作事项", "完成情况", "关键产出/数据", "状态"],
                template_text="| 2 | [事项名称] | [已完成/进行中/延期] | [具体结果] | ✅/🔄/⚠️ |",
            ),
            TemplateSlot(
                key="table.risk.0",
                kind="table_row",
                paragraph_index=6,
                section="二、本周遇到的问题与风险",
                columns=["问题/风险", "影响程度", "已采取动作", "需要支持"],
                template_text="| [问题描述] | 高/中/低 | [已尝试的解决方案] | [需要谁、提供什么资源] |",
            ),
            TemplateSlot(
                key="bullet.think.0",
                kind="bullet",
                paragraph_index=7,
                section="四、思考与建议（可选）",
                template_text="- **流程优化：** [发现的问题 + 建议]",
            ),
        ],
    )


def test_clean_text_drops_pronouns_and_private_life():
    assert clean_text("我完成了官网部署") == "完成了官网部署"
    assert clean_text("他负责对接接口") == "负责对接接口"
    assert clean_text("孩子生病请假一天") == ""
    assert clean_text("周末去看了电影") == ""


def test_person_and_period_fields_are_filled_deterministically():
    values = build_slot_values(
        spec=_spec(),
        draft_values={},
        person_name="张三",
        window=_window(),
    )

    assert values["field.汇报人"] == "张三"
    assert values["field.周期"] == "上一周周报（2026-09-28 至 2026-10-04）"
    # A field the archive cannot answer stays explicit instead of invented.
    assert values["field.部门_项目"] == NOT_INFERRED
    # The template's own title is never rewritten.
    assert "title" not in values


def test_table_rows_are_shaped_like_the_template_columns():
    values = build_slot_values(
        spec=_spec(),
        draft_values={
            "table.work.0": ["1", "完成官网部署", "已完成", "上线 1 个站点", "✅"],
            "table.work.1": ["推进支付模块联调"],
        },
        person_name="张三",
        window=_window(),
    )

    assert values["table.work.0"] == ["1", "完成官网部署", "已完成", "上线 1 个站点", "✅"]
    # A short row keeps the template's fixed label column and pads the rest.
    assert values["table.work.1"] == ["2", "推进支付模块联调", "", "", ""]


def test_empty_section_is_announced_once_with_wu():
    values = build_slot_values(
        spec=_spec(),
        draft_values={},
        person_name="张三",
        window=_window(),
    )

    assert values["table.risk.0"] == [EMPTY_SECTION] * 4
    assert values["bullet.think.0"] == EMPTY_SECTION
    # The first work row says 无; the surplus rows keep the label column only.
    assert values["table.work.0"] == ["1", EMPTY_SECTION, EMPTY_SECTION, EMPTY_SECTION, EMPTY_SECTION]
    assert values["table.work.1"] == ["2", "", "", "", ""]


def test_private_life_rows_do_not_reach_the_document():
    values = build_slot_values(
        spec=_spec(),
        draft_values={
            "table.work.0": ["1", "孩子生病请假", "已完成", "无", "✅"],
            "bullet.think.0": "我建议优化流程，孩子下周开学",
        },
        person_name="张三",
        window=_window(),
    )

    assert values["table.work.0"] == ["1", EMPTY_SECTION, EMPTY_SECTION, EMPTY_SECTION, EMPTY_SECTION]
    assert values["bullet.think.0"] == EMPTY_SECTION


def test_a_field_the_archive_cannot_answer_says_so_instead_of_wu():
    spec = _spec()
    spec.slots.append(
        TemplateSlot(
            key="field.部门_项目",
            kind="field",
            paragraph_index=8,
            section="",
            label="部门/项目",
            template_text="**部门/项目：** [部门或项目名称]",
        )
    )

    values = build_slot_values(
        spec=spec,
        draft_values={"field.部门_项目": "无"},
        person_name="张三",
        window=_window(),
    )

    assert values["field.部门_项目"] == NOT_INFERRED


def test_model_supplied_placeholder_rows_collapse_to_blank_cells():
    values = build_slot_values(
        spec=_spec(),
        draft_values={
            "table.work.0": ["1", "完成官网部署", "已完成", "上线 1 个站点", "✅"],
            "table.work.1": ["2", "无", "无", "无", "无"],
        },
        person_name="张三",
        window=_window(),
    )

    # The section has content, so the surplus row keeps its label and no 无.
    assert values["table.work.1"] == ["2", "", "", "", ""]


def test_bullets_lose_the_marker_and_label_the_template_owns():
    values = build_slot_values(
        spec=_spec(),
        draft_values={"bullet.think.0": "- **支付模块：** 建议把部署脚本固化下来"},
        person_name="张三",
        window=_window(),
    )

    assert values["bullet.think.0"] == "建议把部署脚本固化下来"


def test_no_information_sentences_fold_to_wu():
    values = build_slot_values(
        spec=_spec(),
        draft_values={
            "bullet.think.0": "无发现的问题与建议。",
            "table.risk.0": "无具体问题，无风险说明",
        },
        person_name="张三",
        window=_window(),
    )

    assert values["bullet.think.0"] == EMPTY_SECTION
    assert values["table.risk.0"] == [EMPTY_SECTION] * 4


def test_a_real_negation_is_not_folded():
    values = build_slot_values(
        spec=_spec(),
        draft_values={"bullet.think.0": "无法在本周完成接口联调，需要下周继续"},
        person_name="张三",
        window=_window(),
    )

    assert values["bullet.think.0"] == "无法在本周完成接口联调，需要下周继续"
