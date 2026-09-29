"""Wire schema and intent catalog for the task understanding provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.kernel.models import TaskUnderstanding, UnderstandingIntent


@dataclass(frozen=True)
class IntentDefinition:
    name: str
    description: str
    examples: tuple[str, ...]


INTENT_CATALOG: tuple[IntentDefinition, ...] = (
    IntentDefinition(
        name="todo.create",
        description="创建一条我要去做的事：会议、邀约、提醒、待办都算；不要求时间",
        examples=(
            "明天洗衣服",
            "完成登录模块代码",
            "明天下午三点跟张三开评审会",
            "提醒我给妈妈打电话",
        ),
    ),
    IntentDefinition(
        name="knowledge.answer",
        description="基于已有知识回答问题",
        examples=("公司的办公地址是什么", "解释一下这份材料"),
    ),
    IntentDefinition(
        name="web.research",
        description="从公开网页或外部来源检索信息",
        examples=("查一下这个政策的最新版本", "搜索行业公开数据"),
    ),
    IntentDefinition(
        name="document.compare",
        description="比较两份或多份材料并输出差异或结论",
        examples=("把协议要求和公司简介对比", "比较两个版本有哪些变化"),
    ),
    IntentDefinition(
        name="compliance.assess",
        description="判断主体、材料或行为是否符合规则协议",
        examples=("我们公司是否符合这个协议", "这份材料满足申请条件吗"),
    ),
    IntentDefinition(
        name="form.prepare",
        description="读取表单并生成填写草稿或预览",
        examples=("根据资料填写这张申请表", "准备一份表单草稿"),
    ),
    IntentDefinition(
        name="form.submit",
        description="提交已经准备和确认的表单",
        examples=("确认无误后提交申请表", "帮我提交这份表单"),
    ),
)

INTENT_NAMES = frozenset(item.name for item in INTENT_CATALOG)

INTENT_TASK_KINDS: dict[str, Literal["answer", "action", "mixed"]] = {
    "todo.create": "action",
    "knowledge.answer": "answer",
    "web.research": "action",
    "document.compare": "action",
    "compliance.assess": "answer",
    "form.prepare": "action",
    "form.submit": "action",
}

BOUNDARY_INTENTS = frozenset({"non_task", "other_task"})


class UnderstandingIntentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=500)


class TaskUnderstandingDraft(BaseModel):
    """The exact JSON object that an LLM provider must return."""

    model_config = ConfigDict(extra="forbid")

    is_task: bool
    goal: str = Field(min_length=1, max_length=1000)
    task_kind: Literal["answer", "action", "mixed"] | None = None
    intent_candidates: list[UnderstandingIntentDraft] = Field(
        default_factory=list, max_length=8
    )
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("intent_candidates")
    @classmethod
    def _known_intents(
        cls, value: list[UnderstandingIntentDraft]
    ) -> list[UnderstandingIntentDraft]:
        unknown = sorted({item.name for item in value if item.name not in INTENT_NAMES})
        if unknown:
            raise ValueError(
                "unknown intent candidates: "
                + ", ".join(unknown)
                + "; allowed values: "
                + ", ".join(sorted(INTENT_NAMES))
            )
        return value

    @model_validator(mode="after")
    def _non_task_has_no_intents(self) -> "TaskUnderstandingDraft":
        if not self.is_task and self.intent_candidates:
            raise ValueError("is_task=false cannot contain intent_candidates")
        return self

    def to_understanding(self) -> TaskUnderstanding:
        candidates = sorted(
            self.intent_candidates,
            key=lambda item: item.confidence,
            reverse=True,
        )
        return TaskUnderstanding(
            is_task=self.is_task,
            goal=self.goal,
            task_kind=self.task_kind,
            intent_candidates=[
                UnderstandingIntent(
                    name=item.name,
                    confidence=item.confidence,
                    evidence=item.evidence,
                )
                for item in candidates
            ],
            confidence=self.confidence,
            reason=self.reason,
        )


def intent_catalog_text() -> str:
    return "\n".join(
        f"- {item.name}: {item.description}; examples: {'; '.join(item.examples)}"
        for item in INTENT_CATALOG
    )


def intent_task_kind(
    name: str,
) -> Literal["answer", "action", "mixed"]:
    return INTENT_TASK_KINDS.get(name, "action")
