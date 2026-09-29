"""Optional structured-output Planner backed by an OpenAI-compatible model."""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.infrastructure.llm.client import LLMError, parse_json_object
from app.kernel.references import has_references
from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
    TaskUnderstanding,
)


class PlannerClient(Protocol):
    model: str
    last_call_count: int

    def complete(self, messages: list[dict[str, str]]) -> str:
        ...


class PlannerProviderError(RuntimeError):
    classification = "permanent_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.classification = "retryable_error" if retryable else "permanent_error"


class PlannerValidationError(PlannerProviderError):
    """The model answered twice and neither answer became a valid draft.

    Distinct from a transport failure: this is a bad answer, not a missing one.
    Runtime turns it into a clean ``fail`` decision so a hallucinated field name
    cannot escape the planner as an unhandled exception.
    """

    code = "planner_output_invalid"


class PlannedStepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The system prompt names the step ids so a plan can reference them, and a
    # model sometimes echoes that id back inside the step. The Planner mints the
    # real ids itself, so an echoed one is ignored rather than failing the plan.
    step_id: str | None = None
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    steps: list[PlannedStepDraft] = Field(default_factory=list)


class DecisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    # A model that has just watched answer.compose run sometimes helps by
    # restating the finished answer here. The answer already lives in the
    # Observation, so the field is accepted and ignored instead of failing the
    # Task at the last step.
    output: dict[str, Any] | None = None
    required_input: list[str] = Field(default_factory=list)
    unsupported_intents: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_user_confirmation: bool = False
    reason: str | None = None
    steps: list[PlannedStepDraft] = Field(default_factory=list)

    @field_validator("required_input", "unsupported_intents", "warnings", mode="before")
    @classmethod
    def _accept_a_single_string(cls, value: Any) -> Any:
        """A model that writes one item instead of a list meant the same thing.

        Coercing here keeps the intent the model expressed: rejecting the whole
        answer over a missing bracket would fail a Task that had a perfectly
        good ``request_input`` behind it.
        """

        if isinstance(value, str):
            return [value]
        return value


class OpenAICompatiblePlanner:
    name = "llm"

    def __init__(self, client: PlannerClient) -> None:
        self.client = client
        self.model = str(getattr(client, "model", ""))
        self.last_call_count = 0
        self._capabilities: list[CapabilityDescriptor] = []
        # name -> signature validator, injected by the container. Absent in
        # isolation, in which case unusable arguments are not repaired.
        self._validators: dict[str, Any] = {}

    def set_validators(self, validators: dict[str, Any]) -> None:
        self._validators = dict(validators)

    def set_capabilities(
        self, capabilities: list[CapabilityDescriptor]
    ) -> None:
        self._capabilities = list(capabilities)


    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        self._capabilities = list(capabilities)
        # The id is minted before the model is asked, because the prompt has to
        # tell it which step ids the plan will have: a reference the model cannot
        # name is a reference it cannot write.
        plan_id = str(uuid4())
        messages = _plan_messages(
            task, capabilities, observations, constraints, understanding, plan_id
        )
        draft = self._call(PlanDraft, messages)
        plan = Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective=draft.objective,
            steps=_draft_steps(draft.steps, plan_id),
        )
        plan = self._repair_unusable_steps(
            task, plan, capabilities, observations, constraints, understanding
        )
        return self._repair_unresolved_references(
            task, plan, capabilities, observations, constraints, understanding
        )

    def _repair_unusable_steps(
        self,
        task: TaskEnvelope,
        plan: Plan,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None,
    ) -> Plan:
        """Re-asks the model once when a step's arguments are not usable.

        The deterministic planner runs the same signature validation, so an LLM
        plan whose arguments do not match the capability schema would otherwise
        be accepted here and only rejected when the step executes.
        """

        problems = _unusable_steps(plan, self._validators)
        if not problems:
            return plan
        messages = _plan_messages(
            task, capabilities, observations, constraints, understanding, plan.plan_id
        )
        messages = messages + [
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "objective": plan.objective,
                        "steps": [
                            {"capability": step.capability, "arguments": step.arguments}
                            for step in plan.steps
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "role": "user",
                "content": (
                    "上一步生成的计划有步骤参数无法通过 capability 校验："
                    + "；".join(problems)
                    + "。请只使用该 capability 真正接受的字段名，重新输出完整 JSON 计划。"
                ),
            },
        ]
        draft = self._call(PlanDraft, messages)
        return Plan(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            objective=draft.objective,
            steps=_draft_steps(draft.steps, plan.plan_id),
        )

    def _repair_unresolved_references(
        self,
        task: TaskEnvelope,
        plan: Plan,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None,
    ) -> Plan:
        """Re-asks once when a reference could never resolve.

        The syntax is only useful if it names something real: a forward
        reference, a step id from an earlier plan version, or an Observation the
        Task does not have can never be read at execution time. Catching it here
        keeps such a plan from being persisted at all.
        """

        problems = unresolved_references(plan, observations)
        if not problems:
            return plan
        messages = _plan_messages(
            task, capabilities, observations, constraints, understanding, plan.plan_id
        )
        messages = messages + [
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "objective": plan.objective,
                        "steps": [
                            {"capability": step.capability, "arguments": step.arguments}
                            for step in plan.steps
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "role": "user",
                "content": (
                    "上一步生成的计划有无法解析的引用："
                    + "；".join(problems)
                    + "。引用只能指向本计划中更早的步骤"
                    "（$steps.<step_id>.output.<field>，step_id 按 system 里的规则生成），"
                    "或本任务已有的 Observation"
                    "（$observations.<observation_id>.output.<field>）。请重新输出完整 JSON 计划。"
                ),
            },
        ]
        draft = self._call(PlanDraft, messages)
        repaired = Plan(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            objective=draft.objective,
            steps=_draft_steps(draft.steps, plan.plan_id),
        )
        still = unresolved_references(repaired, observations)
        if still:
            raise PlannerValidationError(
                "plan contains unresolvable references: " + "；".join(still)
            )
        return repaired

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        # Same reason as create_plan: a re-plan also mints step ids, and the
        # model has to be able to name them when a later step reads an earlier one.
        plan_id = str(uuid4())
        messages = _decision_messages(
            task,
            current_plan,
            self._capabilities,
            observations,
            constraints,
            understanding,
            plan_id,
        )
        try:
            draft = self._call(DecisionDraft, messages)
        except PlannerValidationError as exc:
            # The model answered twice and neither answer fit the schema.
            # Escaping as an exception would fail the Task with a stack trace;
            # a fail decision keeps the reason readable and the Task state honest.
            return PlannerDecision(
                action="fail",
                reason=f"planner output could not be parsed: {exc}".strip(),
            )
        decision = _decision_from_draft(
            draft, task, current_plan, observations, plan_id
        )
        problems = (
            unresolved_references(decision.plan, observations)
            if decision.plan is not None
            else []
        )
        if not problems:
            return decision
        messages = messages + [
            {
                "role": "assistant",
                "content": json.dumps(_decision_echo(draft), ensure_ascii=False),
            },
            {
                "role": "user",
                "content": (
                    "上一步返回的 steps 有无法解析的引用："
                    + "；".join(problems)
                    + "。引用只能指向本计划中更早的步骤或本任务已有的 Observation，"
                    "请重新输出完整 JSON 决策。"
                ),
            },
        ]
        try:
            repaired = self._call(DecisionDraft, messages)
        except PlannerValidationError as exc:
            return PlannerDecision(
                action="fail",
                reason=f"planner output could not be parsed: {exc}".strip(),
            )
        decision = _decision_from_draft(
            repaired, task, current_plan, observations, plan_id
        )
        if decision.plan is not None:
            still = unresolved_references(decision.plan, observations)
            if still:
                return PlannerDecision(
                    action="fail",
                    reason="plan contains unresolvable references: "
                    + "；".join(still),
                )
        return decision

    def _call(self, model_type, messages: list[dict[str, str]]):
        self.last_call_count = 0
        try:
            raw = self.client.complete(messages)
        except LLMError as exc:
            raise PlannerProviderError(
                str(exc), retryable=exc.classification == "retryable_error"
            ) from exc
        self.last_call_count += max(int(getattr(self.client, "last_call_count", 1)), 1)
        last_error = ""
        for attempt in range(2):
            try:
                return model_type.model_validate(parse_json_object(raw))
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                if attempt:
                    break
                try:
                    raw = self.client.complete(
                        [
                            *messages,
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": (
                                    "只返回修正后的 JSON 对象，字段必须符合要求。"
                                    f"错误：{last_error}"
                                ),
                            },
                        ]
                    )
                except LLMError as exc:
                    raise PlannerProviderError(
                        str(exc),
                        retryable=exc.classification == "retryable_error",
                    ) from exc
                self.last_call_count += max(
                    int(getattr(self.client, "last_call_count", 1)), 1
                )
        raise PlannerValidationError(f"planner output failed validation: {last_error}")



def _unusable_steps(plan: Plan, validators: dict[str, Any]) -> list[str]:
    """One message per step whose arguments fail its own capability schema.

    A step that uses a reference (dollar-steps output placeholder) is skipped:
    the placeholder cannot be validated before Runtime resolves it.
    """

    problems: list[str] = []
    for step in plan.steps:
        if has_references(step.arguments):
            continue
        validator = validators.get(step.capability)
        if validator is None:
            continue
        try:
            validator(step.arguments)
        except Exception as exc:
            problems.append(f"{step.capability}: {exc}")
    return problems


def _draft_steps(steps: list[PlannedStepDraft], plan_id: str) -> list[PlanStep]:
    return [
        PlanStep(
            step_id=f"{plan_id}-step-{index}",
            plan_id=plan_id,
            order=index,
            capability=item.capability,
            arguments=dict(item.arguments),
        )
        for index, item in enumerate(steps, start=1)
    ]


def _decision_echo(draft: DecisionDraft) -> dict[str, Any]:
    """The model's own answer, echoed back when a repair round is needed."""

    return {
        "action": draft.action,
        "reason": draft.reason,
        "required_input": list(draft.required_input),
        "steps": [
            {"capability": item.capability, "arguments": item.arguments}
            for item in draft.steps
        ],
    }


def _decision_from_draft(
    draft: DecisionDraft,
    task: TaskEnvelope,
    current_plan: Plan,
    observations: list[Observation],
    plan_id: str,
) -> PlannerDecision:
    """Turns a validated draft into a decision, degrading to fail when lost."""

    if draft.action not in {
        "continue",
        "replan",
        "request_input",
        "complete",
        "fail",
        "unsupported",
    }:
        # A model that names an action outside the enum is confused, not
        # broken. Failing the Task with a readable reason beats raising.
        return PlannerDecision(
            action="fail",
            reason=f"planner returned an unknown action: {draft.action}",
        )
    plan = None
    if draft.steps:
        plan = Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            parent_plan_id=current_plan.plan_id,
            triggered_by_observation_id=observations[-1].observation_id
            if observations
            else None,
            objective=current_plan.objective,
            steps=_draft_steps(draft.steps, plan_id),
        )
    return PlannerDecision(
        action=draft.action,
        plan=plan,
        required_input=list(draft.required_input),
        unsupported_intents=list(draft.unsupported_intents),
        warnings=list(draft.warnings),
        requires_user_confirmation=draft.requires_user_confirmation,
        reason=draft.reason,
    )


def unresolved_references(plan: Plan, observations: list[Observation]) -> list[str]:
    """Every reference in the plan that can never resolve, verbatim.

    A $steps reference must name a step that comes earlier *in this plan*:
    the output it points at has to exist by the time the step runs. A
    $observations reference must name an Observation this Task already has,
    which is the only way to reach a result from an earlier plan version.
    """

    known_observations = {item.observation_id for item in observations}
    earlier: set[str] = set()
    problems: list[str] = []
    for step in sorted(plan.steps, key=lambda item: item.order):
        for reference in _iter_references(step.arguments):
            parts = reference.split(".")
            if len(parts) < 4 or parts[2] != "output":
                problems.append(
                    f"{reference}（{step.step_id} 里的引用格式应为 "
                    "$steps.<step_id>.output.<field>）"
                )
            elif parts[0] == "$steps":
                if parts[1] not in earlier:
                    problems.append(
                        f"{reference}（{step.step_id} 引用的 {parts[1]} "
                        "不是本计划中更早的步骤）"
                    )
            elif parts[0] == "$observations":
                if parts[1] not in known_observations:
                    problems.append(
                        f"{reference}（{step.step_id} 引用的 Observation "
                        f"{parts[1]} 不存在）"
                    )
            else:
                problems.append(
                    f"{reference}（{step.step_id} 使用了未知的引用前缀 {parts[0]}）"
                )
        earlier.add(step.step_id)
    return problems


def _iter_references(value: Any) -> list[str]:
    """Every string in the arguments that claims to be a reference."""

    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_iter_references(item))
        return found
    if isinstance(value, dict):
        found = []
        for item in value.values():
            found.extend(_iter_references(item))
        return found
    return []

def _plan_messages(
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
    observations: list[Observation],
    constraints: PlanningConstraints,
    understanding: TaskUnderstanding | None,
    plan_id: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是动态 Planner。只输出 JSON。"
                "只能从给定 capabilities 中选择步骤，不能执行工具。"
                "输出格式：{\"objective\": string, \"steps\": "
                "[{\"capability\": string, \"arguments\": object}]}。"
                "步骤按顺序执行，你给出的顺序就是执行顺序。"
                f"本计划的 plan_id 是 \"{plan_id}\"，本计划所有 step_id 形如 \"{plan_id}-step-<n>\"，"
                f"第 k 步就是 \"{plan_id}-step-k\"。"
                "steps 里每个对象只能有 capability 与 arguments 两个字段，不要写 step_id。"
                "后一步要引用前一步的输出，必须写成 \"$steps.<step_id>.output.<field>\"，"
                f"例如第一步是 \"$steps.{plan_id}-step-1.output.content\"；"
                "引用只能指向本计划里更早的步骤。引用历史结果用 "
                "\"$observations.<observation_id>.output.<field>\"。"
                "不要编造 field 名：每条 capability 的 input_schema / output_schema "
                "就是它的字段表。要把来源证据交给 answer.compose 时，把上游输出的 "
                "evidence 原样传过去：\"evidence\": \"$steps.<step_id>.output.evidence\"。"
                "也不要用 {{...}} 之类的模板语法。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": task.model_dump(mode="json"),
                    "understanding": understanding.model_dump(mode="json")
                    if understanding
                    else None,
                    "capabilities": [item.model_dump(mode="json") for item in capabilities],
                    "observations": [
                        item.model_dump(mode="json") for item in observations
                    ],
                    "constraints": constraints.model_dump(mode="json"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _decision_messages(
    task: TaskEnvelope,
    current_plan: Plan,
    capabilities: list[CapabilityDescriptor],
    observations: list[Observation],
    constraints: PlanningConstraints,
    understanding: TaskUnderstanding | None,
    plan_id: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是动态 Planner。只输出 JSON。"
                "action 只能是 continue、replan、request_input、complete、fail、unsupported。"
                "replan 时可以输出 steps，steps 只能使用当前 capabilities。"
                f"如果你带 steps，这些新步骤的 plan_id 是 \"{plan_id}\"，第 k 步的 step_id 是 "
                f"\"{plan_id}-step-k\"，steps 里每个对象只能有 capability 与 arguments 两个字段；"
                "步骤之间引用前一步输出写 "
                "\"$steps.<step_id>.output.<field>\"，只能指向本计划更早的步骤；"
                "引用历史结果写 \"$observations.<observation_id>.output.<field>\"。"
                "steps[].capability 必须是 capabilities 列表里真实存在的名称；"
                "request_input、complete、fail 这些是 action，不是能力，绝对不要写进 steps。"
                "需要用户补充信息时，用 action=request_input 并在顶层填 required_input，steps 留空。"
                "不得自动重复 unknown 外部结果。"
                "内核已经替你把 retryable_error 原地重试过了，只有重试预算用尽才会轮到你判断；"
                "历史里出现过 retryable_error 不代表现在还失败——先看最后一条 observation 的状态，"
                "最后一条是 succeeded 就说明重试已经成功，继续或收尾即可。"
                "replan 的含义是换一条路：换能力，或换同一能力的参数，不是把同一个调用再排一遍。"
                "如果当前计划仍然成立、剩下的步骤仍然适用，必须返回 continue；"
                "不要把剩下的步骤原样再输出一遍，那会白花一次规划。"
                "只有计划的前提失效时才用它——参数被能力拒绝、对象或文件不存在、"
                "某一步的结果推翻了后面的计划、计划里需要一个不存在的能力。"
                "observation.error.classification=permanent_error 表示重试同一能力不会有不同结果；"
                "此时必须改换手段，或 action=request_input，不得再次计划同一个调用。"
                "新计划的 steps 不得重复已经成功或已经失败过的「能力+参数」组合。"
                "replan 必须给出至少一个 steps；如果你找不到任何没试过且能推进的步骤，"
                "不要用 replan 表达「再想想」，那会变成空计划。改用 action=fail，"
                "并在 reason 里说明为什么现有能力做不到；需要用户决策时用 request_input。"
                "顶层字段只能是 action、steps、required_input、unsupported_intents、"
                "warnings、requires_user_confirmation、reason，不要输出其它字段。"
                "最终回答由 answer.compose 步骤产生，不需要你在决策里再写一遍。"
                "reason 必须填写：一句话说明你为什么这样判断。"
                "只读且独立的部分可以完成并写 warnings。"
                "不支持的意图如果是写步骤前置条件，必须 fail 或 request_input。"
                "涉及外部副作用的部分执行必须 requires_user_confirmation=true。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": task.model_dump(mode="json"),
                    "understanding": understanding.model_dump(mode="json")
                    if understanding
                    else None,
                    "current_plan": current_plan.model_dump(mode="json"),
                    "capabilities": [
                        item.model_dump(mode="json") for item in capabilities
                    ],
                    "observations": [
                        item.model_dump(mode="json") for item in observations
                    ],
                    "constraints": constraints.model_dump(mode="json"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
