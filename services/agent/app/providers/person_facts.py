"""Turn person-scoped messages into attributed facts.

The extraction model is deliberately narrower than the answer model: it only
reports facts about the subject, keeps the original quote and message handle,
and marks whether the subject said it or somebody else did. The caller merges
the batches and lets the answer model speak; this layer never writes prose.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.infrastructure.llm.client import LLMError, parse_json_object


class PersonFactError(RuntimeError):
    classification = "permanent_error"
    code = "person_fact_failed"


class PersonFactUnavailable(PersonFactError):
    classification = "retryable_error"
    code = "person_fact_unavailable"


class PersonFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact_type: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=80)
    value: str = Field(min_length=1, max_length=1000)
    quote: str = Field(default="", max_length=1000)
    speaker_role: str = Field(default="other", pattern="^(subject|other)$")
    resource_id: str = Field(default="", max_length=128)
    sent_at: str = Field(default="", max_length=80)


class PersonFactBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[PersonFact] = Field(default_factory=list)


class PersonFactProvider(Protocol):
    def extract(
        self,
        *,
        subject: str,
        aliases: list[str],
        question: str,
        messages: list[dict[str, Any]],
    ) -> list[PersonFact]:
        ...


SYSTEM_PROMPT = (
    "你是人物资料抽取器。只从输入的消息中抽取关于『目标人物』的明确事实。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象：{\"facts\":[{\"fact_type\":string,\"label\":string,"
    "\"value\":string,\"quote\":string,\"speaker_role\":\"subject\"|\"other\","
    "\"resource_id\":string}]}。\n"
    "2. 只抽取目标人物本人的信息；不要把对话另一方（提问者或其他群成员）"
    "的信息、观点、经历当成目标人物的事实。\n"
    "3. aliases 中的名字都属于同一个目标人物；消息 sender_name 命中 aliases "
    "时，该消息视为目标人物本人发出。speaker_role=subject 表示这句话由目标人物本人发出；"
    "speaker_role=other 表示由别人发出。"
    "别人的话只有在明确描述目标人物时才可以输出，并且必须标为 other。\n"
    "4. quote 必须是消息里的原文片段，不能改写，不能拼接。"
    "resource_id 填该消息的 resource_id。\n"
    "5. 只写消息里明确出现的事实，不推断，不补全。玩笑、戏称照录，"
    "可在 label 中体现，但不要当成真实身份。\n"
    "6. fact_type 建议使用：identity、nickname、gender、student_id、phone、"
    "email、address、role、school、company、course、project、event、preference、other。\n"
    "7. 必须优先使用上面的具体类别，只有确实无法归入任何具体类别时才使用 other。"
    "项目、开发、任务、职责、工作安排使用 project/work/task/role；"
    "习惯、喜好、倾向使用 preference；近期发生的活动使用 event/course。\n"
    "8. 没有可抽取事实时返回 {\"facts\":[]}。"
)


class LlmPersonFactProvider:
    def __init__(self, client) -> None:
        self.client = client
        self.model = str(getattr(client, "model", ""))
        self._call_count: ContextVar[int] = ContextVar(
            f"agent_person_fact_calls_{id(self)}", default=0
        )
        self._last_error = ""

    @property
    def last_call_count(self) -> int:
        return max(int(self._call_count.get()), 0)

    @last_call_count.setter
    def last_call_count(self, value: int) -> None:
        self._call_count.set(max(int(value), 0))

    def extract(
        self,
        *,
        subject: str,
        aliases: list[str],
        question: str,
        messages: list[dict[str, Any]],
    ) -> list[PersonFact]:
        if not messages:
            return []
        self.last_call_count = 0
        prompt = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "subject": subject,
                        "aliases": aliases,
                        "question": question,
                        "messages": messages,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        raw = self._complete(prompt)
        batch = self._parse(raw)
        if batch is None:
            repaired = self._complete(
                prompt
                + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "上一个输出不是合法 JSON。只返回修正后的 JSON 对象，"
                            "结构为 {\"facts\":[...]}。"
                            f"错误：{self._last_error}"
                        ),
                    },
                ]
            )
            batch = self._parse(repaired)
        if batch is None:
            raise PersonFactError(
                f"person fact output failed after repair: {self._last_error}"
            )
        return batch.facts

    def _complete(self, messages: list[dict[str, str]]) -> str:
        try:
            raw = self.client.complete(messages)
        except LLMError as exc:
            error = (
                PersonFactUnavailable
                if exc.classification == "retryable_error"
                else PersonFactError
            )
            raise error(str(exc)) from exc
        self.last_call_count += max(int(getattr(self.client, "last_call_count", 1)), 1)
        return raw

    def _parse(self, raw: str) -> PersonFactBatch | None:
        try:
            return PersonFactBatch.model_validate(parse_json_object(raw))
        except (ValueError, ValidationError) as exc:
            self._last_error = str(exc)
            return None
