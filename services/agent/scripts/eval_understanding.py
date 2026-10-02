"""Replays the labelled corpus against the real understanding model.

The offline suite (tests/test_understanding_corpus.py) pins the deterministic
parts. This script measures the part only a model can answer: does the provider
pick the right intent for real wording. It is intentionally NOT part of pytest
because it needs a live LLM and costs money.

Run from services/agent:

    ./.venv/Scripts/python.exe scripts/eval_understanding.py
    ./.venv/Scripts/python.exe scripts/eval_understanding.py --source collected
    ./.venv/Scripts/python.exe scripts/eval_understanding.py --verbose --json out.json

Exit code is 0 when every case matches, 1 otherwise, so it can gate a rollout.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from app.config import Settings  # noqa: E402
from app.container import build_understanding_provider  # noqa: E402
from app.ingress.chat import ChatIngress  # noqa: E402
from app.ingress.knowledge_events import KnowledgeEventIngress  # noqa: E402

CORPUS_PATH = ROOT / "tests" / "fixtures" / "understanding_corpus.json"


def load_cases(source: str | None) -> list[dict]:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    return [c for c in cases if source is None or c["source"] == source]


def threshold_for(settings: Settings, source: str) -> float:
    if source == "knowledge_event":
        return float(settings.understanding_min_confidence_collected)
    return float(settings.understanding_min_confidence)


def evaluate(provider, settings: Settings, case: dict) -> dict:
    """Runs one case and returns the observed verdict plus the mismatches."""

    collected = case["source"] == "collected"
    source_type = "knowledge_event" if collected else "chat"
    threshold = threshold_for(settings, source_type)

    if collected:
        task = ChatIngress().create_task("eval-user", {"text": case["text"]})
        task = task.model_copy(update={"source_type": "knowledge_event", "source_ref": {}})
    else:
        task = ChatIngress().create_task("eval-user", {"text": case["text"]})

    try:
        understanding = provider.understand(task, min_confidence=threshold)
    except TypeError:
        understanding = provider.understand(task)
    observed_task = bool(understanding.is_task)
    observed = [item.name for item in understanding.intent_candidates]

    problems: list[str] = []
    # is_task may be None: adversarial inputs only assert that no injected
    # intent leaked out, not what the verdict should be.
    expected_task = case["is_task"]
    if expected_task is not None and observed_task is not expected_task:
        problems.append(f"is_task {observed_task} != {case['is_task']}")
    missing = [n for n in case["expect"] if n not in observed]
    if missing:
        problems.append(f"missing {missing}")
    forbidden = [n for n in case["forbid"] if n in observed]
    if forbidden:
        problems.append(f"forbidden {forbidden}")
    if case["source"] == "collected":
        gate_ok = KnowledgeEventIngress().task_candidate_hint(case["text"])
        if gate_ok is not (case["gate"] == "pass"):
            problems.append(f"gate {gate_ok} != {case['gate']}")

    return {
        "id": case["id"],
        "text": case["text"],
        "tags": case["tags"],
        "source": case["source"],
        "threshold": threshold,
        "expected_is_task": case["is_task"],
        "observed_is_task": observed_task,
        "expected": case["expect"],
        "observed": observed,
        "reason": understanding.reason,
        "decision_source": getattr(provider, "last_decision_source", None),
        "fallback_reason": getattr(provider, "last_fallback_reason", None),
        "laya_answer_confidence": getattr(provider, "last_answer_confidence", None),
        "laya_margin": getattr(provider, "last_margin", None),
        "ok": not problems,
        "problems": problems,
    }


def compare(
    settings: Settings,
    cases: list[dict],
    *,
    verbose: bool,
    sleep_seconds: float,
) -> list[dict]:
    laya_settings = replace(settings, understanding_provider="laya")
    llm_settings = replace(settings, understanding_provider="llm")
    laya_provider = build_understanding_provider(laya_settings)
    llm_provider = build_understanding_provider(llm_settings)

    results: list[dict] = []
    for case in cases:
        laya_result = evaluate(laya_provider, laya_settings, case)
        llm_result = evaluate(llm_provider, llm_settings, case)
        agreement = (
            laya_result["observed_is_task"] == llm_result["observed_is_task"]
            and laya_result["observed"] == llm_result["observed"]
        )
        result = {
            "id": case["id"],
            "text": case["text"],
            "tags": case["tags"],
            "agreement": agreement,
            "laya": laya_result,
            "llm": llm_result,
        }
        results.append(result)
        if verbose or not agreement:
            print(
                f"{'same' if agreement else 'DIFF'} "
                f"{case['id']:<28} "
                f"laya={laya_result['observed']} "
                f"llm={llm_result['observed']}"
            )
        time.sleep(sleep_seconds)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["collected", "chat"], default=None)
    parser.add_argument("--verbose", action="store_true", help="print every case")
    parser.add_argument(
        "--provider",
        choices=["llm", "laya", "jev", "hybrid"],
        default=None,
        help="override AGENT_UNDERSTANDING_PROVIDER for this evaluation",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="run Laya and LLM independently and report their agreement",
    )
    parser.add_argument("--json", default="", help="write the raw results to this path")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    settings = Settings()
    if args.provider:
        settings = replace(settings, understanding_provider=args.provider)
    cases = load_cases(args.source)

    if args.compare:
        results = compare(
            settings,
            cases,
            verbose=args.verbose,
            sleep_seconds=args.sleep,
        )
        agreements = sum(1 for result in results if result["agreement"])
        print()
        print(f"laya/llm agreement: {agreements}/{len(results)}")
        if args.json:
            Path(args.json).write_text(
                json.dumps(results, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"raw results -> {args.json}")
        return 0 if agreements == len(results) else 1

    if settings.understanding_provider not in {"llm", "laya", "jev", "hybrid"}:
        print(
            "AGENT_UNDERSTANDING_PROVIDER=%s; use --provider to select "
            "llm, laya, jev, or hybrid"
            % settings.understanding_provider
        )
        return 2

    provider = build_understanding_provider(settings)
    print(f"provider={settings.understanding_provider} "
          f"cases={len(cases)} "
          f"chat>={settings.understanding_min_confidence} "
          f"collected>={settings.understanding_min_confidence_collected}")
    print()

    results = []
    for case in cases:
        result = evaluate(provider, settings, case)
        results.append(result)
        mark = "ok  " if result["ok"] else "FAIL"
        if args.verbose or not result["ok"]:
            identifier = result["id"]
            expected = result["expected"]
            observed = result["observed"]
            print(f"{mark} {identifier:<28} exp={expected} got={observed}")
            if result["problems"]:
                print(f"       {result['text']}")
                for problem in result["problems"]:
                    print(f"       -> {problem}")
        time.sleep(args.sleep)

    failed = [r for r in results if not r["ok"]]
    total = len(results)
    print()
    print(f"cases     : {total - len(failed)}/{total} pass")

    # is_task is the gate that decides whether anything happens at all.
    asserted = [r for r in results if r["expected_is_task"] is not None]
    task_ok = sum(1 for r in asserted if r["expected_is_task"] == r["observed_is_task"])
    print(f"is_task   : {task_ok}/{len(asserted)}" + (f" (+{total - len(asserted)} not asserted)" if len(asserted) != total else ""))

    # Intent exact match, only over cases that actually expect an intent.
    intent_cases = [r for r in results if r["expected"]]
    intent_ok = sum(1 for r in intent_cases if set(r["expected"]) <= set(r["observed"]))
    print(f"intent    : {intent_ok}/{len(intent_cases)} (expected intents all present)")

    false_positives = [
        r for r in results
        if r["expected_is_task"] is False and r["observed_is_task"] is True
    ]
    false_negatives = [
        r for r in results
        if r["expected_is_task"] is True and r["observed_is_task"] is False
    ]
    false_positive_ids = [r["id"] for r in false_positives]
    false_negative_ids = [r["id"] for r in false_negatives]
    print(f"false pos : {len(false_positives)}" + (f" {false_positive_ids}" if false_positives else ""))
    print(f"false neg : {len(false_negatives)}" + (f" {false_negative_ids}" if false_negatives else ""))

    by_tag: dict[str, list[bool]] = {}
    for result in results:
        for tag in result["tags"]:
            by_tag.setdefault(tag, []).append(result["ok"])
    print()
    for tag in sorted(by_tag):
        values = by_tag[tag]
        print(f"  {tag:<18} {sum(values)}/{len(values)}")

    if failed:
        print()
        print("FAILED CASES")
        for result in failed:
            print(f"  {result['id']:<28} {result['text']}")
            print(f"      expected={result['expected']} is_task={result['expected_is_task']}")
            print(f"      observed={result['observed']} is_task={result['observed_is_task']}")
            print(f"      reason={result['reason']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print()
        print(f"raw results -> {args.json}")

    print()
    print("RESULT:", "PASS" if not failed else f"FAIL ({len(failed)} case(s))")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
