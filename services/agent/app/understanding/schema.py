"""Wire schema and intent catalog for the task understanding provider.

Two vocabularies live here. An *intent* is what the user wants (a stable product
concept); a *capability* is what this deployment can actually execute. They are
deliberately not the same list, so the catalog never has to be renamed when an
implementation changes. What they do share is availability: an intent whose
``requires`` capabilities are not registered must not be offered to any
classifier, or the runtime ends up planning work no tool can do.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.kernel.models import TaskUnderstanding, UnderstandingIntent


@dataclass(frozen=True)
class IntentDefinition:
    name: str
    # Chinese description rendered into the LLM understanding prompt.
    description: str
    # English rubric rendered into the Laya / Jev choice question. Those
    # checkpoints follow English option descriptions far more reliably than
    # Chinese ones, so the two languages are kept side by side on purpose.
    criteria: str
    examples: tuple[str, ...]
    # Capabilities that must all be registered for this intent to be offered.
    # ``None`` means no capability implements this intent yet, so it is never
    # offered; an empty frozenset means it is always available.
    requires: frozenset[str] | None = None


INTENT_CATALOG: tuple[IntentDefinition, ...] = (
    IntentDefinition(
        name="todo.create",
        description="创建一条我要去做的事：会议、邀约、提醒、待办都算；不要求时间",
        criteria=(
            "Create a personal to-do, meeting, invitation, reminder, or future "
            "action item. Statements like I will do something tomorrow count. "
            "Work items and everyday errands count too: writing a report, "
            "buying something, picking up a package, moving house."
        ),
        examples=(
            "明天洗衣服",
            "完成登录模块代码",
            "明天下午三点跟张三开评审会",
            "提醒我给妈妈打电话",
        ),
        requires=frozenset({"todo.create"}),
    ),
    IntentDefinition(
        name="knowledge.answer",
        description=(
            "基于已有知识回答问题；公司内部的项目、系统、服务器、部署、任务、"
            "进度、负责人和自家官网都归这里"
        ),
        criteria=(
            "Answer a question from existing company or internal knowledge. "
            "Questions about internal projects, systems, servers, deployments, "
            "tasks, progress, owners, or the company's own official website "
            "belong here."
        ),
        examples=("公司的办公地址是什么", "解释一下这份材料"),
        requires=frozenset({"knowledge.search_content"}),
    ),
    IntentDefinition(
        name="web.research",
        description=(
            "从公开网页或外部来源检索信息；只有明确提到公开、互联网、新闻或链接时才用，"
            "公司内部实体本身不算"
        ),
        criteria=(
            "Search the public internet or external sources for information. "
            "Use this only when the user explicitly asks for public internet "
            "sources, public news, public announcements, external links, or "
            "gives a URL. A bare internal entity name such as the company's own "
            "official website is knowledge.answer, not web.research."
        ),
        examples=("查一下这个政策的最新版本", "搜索行业公开数据"),
        requires=frozenset({"web.search"}),
    ),
    IntentDefinition(
        name="document.compare",
        description="比较两份或多份材料并输出差异或结论",
        criteria="Compare two or more documents and report their differences.",
        examples=("把协议要求和公司简介对比", "比较两个版本有哪些变化"),
    ),
    IntentDefinition(
        name="compliance.assess",
        description="判断主体、材料或行为是否符合规则协议",
        criteria=(
            "Assess whether a person, document, or action complies with a rule "
            "or agreement."
        ),
        examples=("我们公司是否符合这个协议", "这份材料满足申请条件吗"),
    ),
    IntentDefinition(
        name="form.prepare",
        description="读取表单并生成填写草稿或预览",
        criteria="Read a form and prepare a draft or preview before submission.",
        examples=("根据资料填写这张申请表", "准备一份表单草稿"),
    ),
    IntentDefinition(
        name="form.submit",
        description="提交已经准备和确认的表单",
        criteria="Submit an already prepared and confirmed form.",
        examples=("确认无误后提交申请表", "帮我提交这份表单"),
    ),
)

INTENT_NAMES = frozenset(item.name for item in INTENT_CATALOG)

# Boundary labels are not intents: they describe why no intent applies. They are
# always offered to a choice classifier and never appear in the LLM catalog.
BOUNDARY_CRITERIA: dict[str, str] = {
    "non_task": (
        "Chit-chat, greetings, opinions, complaints, examples, hypotheses, "
        "completed past actions, or no clear goal."
    ),
    "other_task": (
        "A clear task that does not fit any category above, such as booking a "
        "train ticket."
    ),
}


def _partition_intents(
    capability_names: Iterable[str],
) -> tuple[tuple[IntentDefinition, ...], tuple[IntentDefinition, ...]]:
    available = frozenset(
        str(name).strip() for name in capability_names if str(name).strip()
    )
    offered: list[IntentDefinition] = []
    withheld: list[IntentDefinition] = []
    for item in INTENT_CATALOG:
        if item.requires is not None and item.requires <= available:
            offered.append(item)
        else:
            withheld.append(item)
    return tuple(offered), tuple(withheld)


def available_intents(
    capability_names: Iterable[str],
) -> tuple[IntentDefinition, ...]:
    """Intents whose required capabilities are all registered right now."""

    return _partition_intents(capability_names)[0]


def unavailable_intents(
    capability_names: Iterable[str],
) -> tuple[IntentDefinition, ...]:
    """Intents this deployment cannot execute, kept for logs and diagnostics."""

    return _partition_intents(capability_names)[1]


def choice_criteria(
    intents: Iterable[IntentDefinition] | None = None,
) -> dict[str, str]:
    """The option map for a Laya / Jev ``choice`` question."""

    selected = INTENT_CATALOG if intents is None else tuple(intents)
    criteria = {item.name: item.criteria for item in selected}
    criteria.update(BOUNDARY_CRITERIA)
    return criteria

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


def intent_catalog_text(intents: Iterable[IntentDefinition] | None = None) -> str:
    selected = INTENT_CATALOG if intents is None else tuple(intents)
    if not selected:
        return "- 当前部署没有可执行的意图；所有任务都应返回 intent_candidates=[]"
    return "\n".join(
        f"- {item.name}: {item.description}; examples: {'; '.join(item.examples)}"
        for item in selected
    )


def intent_task_kind(
    name: str,
) -> Literal["answer", "action", "mixed"]:
    return INTENT_TASK_KINDS.get(name, "action")
