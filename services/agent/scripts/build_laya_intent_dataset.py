"""Build a balanced Chinese intent dataset for Laya head fine-tuning.

The generated rows are synthetic and do not read user traffic. Existing
labelled corpus rows are appended as real seeds.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from app.config import Settings  # noqa: E402
from app.infrastructure.llm.client import OpenAIChatClient, parse_json_object  # noqa: E402

CORPUS_PATH = ROOT / "tests" / "fixtures" / "understanding_corpus.json"
DEFAULT_OUTPUT = ROOT / "tests" / "fixtures" / "laya_intent_dataset.jsonl"

INTENT_PROMPTS = {
    "todo.create": (
        "生成{count}条中文用户消息，表达创建个人待办、会议、邀约、提醒，"
        "或未来要做的事情。必须多样，不要求带时间。"
    ),
    "knowledge.answer": (
        "生成{count}条中文用户消息，用户想基于公司或已有内部知识获得答案。"
    ),
    "web.research": (
        "生成{count}条中文用户消息，用户想搜索互联网、公开网页或外部最新资料。"
    ),
    "document.compare": (
        "生成{count}条中文用户消息，用户想比较两份或多份材料并知道差异。"
    ),
    "compliance.assess": (
        "生成{count}条中文用户消息，用户想判断主体、材料或行为是否符合规则、"
        "协议、条件或要求。"
    ),
    "form.prepare": (
        "生成{count}条中文用户消息，用户想根据资料填写、生成或预览表单草稿，"
        "但不是在正式提交。"
    ),
    "form.submit": (
        "生成{count}条中文用户消息，用户明确要求提交已经确认或准备完成的表单。"
    ),
    "non_task": (
        "生成{count}条中文用户消息，属于闲聊、问候、吐槽、评价、举例、假设、"
        "转述、已完成动作，或没有明确目标。不能是待办或知识问题。"
    ),
    "other_task": (
        "生成{count}条中文用户消息，用户有明确任务目标，但不属于待办、"
        "知识问答、网页检索、材料比较、合规评估、准备表单或提交表单。"
    ),
}

SYSTEM_PROMPT = """你是意图分类数据生成器。
只输出一个 JSON 对象，格式为 {"examples": ["消息1", "消息2"]}。
不要输出 Markdown、解释或额外字段。消息要自然、简短、互不重复。"""


def load_real_seed_rows() -> list[dict[str, str]]:
    cases = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["cases"]
    rows: list[dict[str, str]] = []
    for case in cases:
        expected = list(case.get("expect") or [])
        if expected:
            intent = expected[0]
        elif case.get("is_task") is False:
            intent = "non_task"
        elif case.get("is_task") is True:
            intent = "other_task"
        else:
            continue
        rows.append(
            {
                "text": str(case["text"]).strip(),
                "intent": intent,
                "source": "corpus",
            }
        )
    return rows


def generate_rows(settings: Settings, per_class: int) -> list[dict[str, str]]:
    client = OpenAIChatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=max(30.0, settings.llm_timeout_seconds),
        max_output_tokens=max(1200, settings.llm_max_output_tokens),
        response_format="json_object",
    )
    rows: list[dict[str, str]] = []
    for intent, template in INTENT_PROMPTS.items():
        raw = client.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": template.format(count=per_class)
                    + " 只返回 JSON 对象。",
                },
            ]
        )
        payload = parse_json_object(raw)
        examples = payload.get("examples")
        if not isinstance(examples, list):
            raise ValueError(f"{intent}: generated payload has no examples list")
        seen: set[str] = set()
        for item in examples:
            text = str(item or "").strip()
            if not text or "\n" in text or text in seen:
                continue
            seen.add(text)
            rows.append({"text": text, "intent": intent, "source": "synthetic"})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-class", type=int, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-corpus",
        action="store_true",
        help="write only generated rows; used for an independent evaluation set",
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.llm_base_url or not settings.llm_model:
        print("AGENT_LLM_BASE_URL and AGENT_LLM_MODEL are required")
        return 2

    rows = generate_rows(settings, max(1, args.per_class))
    if not args.no_corpus:
        rows = load_real_seed_rows() + rows
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["intent"]] = counts.get(row["intent"], 0) + 1
    print(
        json.dumps(
            {"output": str(args.output), "rows": len(rows), "counts": counts},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
