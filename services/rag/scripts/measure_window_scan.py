"""Measure window-scan latency and throughput against the configured model.

    python scripts/measure_window_scan.py \
        --conversation-limit 3 --windows-per-conversation 2 \
        --concurrency 1,4,8 --known-entities 20

The measurement uses real messages from the database and the same prompt
builder the worker uses, then calls the configured extraction client. Nothing is
written to the database, so it is safe to run against any environment.

What it answers, from the plan's "上量前必须补测" list:
  - per-window latency and token use on real 20-message windows
  - whether the JSON comes back complete at the configured max_tokens
  - effective tokens/minute at each concurrency level
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.window_scan_service import (  # noqa: E402
    build_extraction_prompt,
    build_windows,
    clean_entities,
    clean_relations,
)
from app.config import settings  # noqa: E402
from app.infrastructure.extraction.client import (  # noqa: E402
    EntityExtractionClient,
    ExtractionError,
)
from app.infrastructure.persistence.mvp import PostgresRagMVPRepository  # noqa: E402


class _RecordingClient:
    """Wraps the real client so token totals can be read back per call."""

    def __init__(self) -> None:
        self.inner = EntityExtractionClient()
        self.calls: list[dict] = []

    def extract(self, prompt: str) -> dict:
        started = time.perf_counter()
        try:
            payload, usage = self.inner.extract_with_usage(prompt)
        except ExtractionError as exc:
            self.calls.append({
                "ok": False, "error": str(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "total_tokens": 0,
            })
            raise
        details = usage.get("completion_tokens_details") or {}
        self.calls.append({
            "ok": True, "latency_ms": int((time.perf_counter() - started) * 1000),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "entities": len(clean_entities(payload.get("entities"))),
            "relations": len(clean_relations(payload.get("relations"))),
        })
        return payload


def _collect_windows(repository, *, conversation_limit: int, per_conversation: int, size: int, step: int):
    windows = []
    for conversation in repository.list_active_conversations(limit=conversation_limit):
        chunks = repository.list_conversation_chunks(
            scope_type=conversation["scope_type"], scope_id=conversation["scope_id"],
            conversation_id=conversation["conversation_id"], limit=max(size, per_conversation * step),
        )
        if not chunks:
            continue
        for window in build_windows(chunks, size, step)[:per_conversation]:
            windows.append((conversation, window))
    return windows


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-limit", type=int, default=3)
    parser.add_argument("--windows-per-conversation", type=int, default=2)
    parser.add_argument("--concurrency", default="1,4,8")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat the window list to reach a larger sample")
    args = parser.parse_args(argv)

    repository = PostgresRagMVPRepository()
    windows = _collect_windows(
        repository, conversation_limit=args.conversation_limit,
        per_conversation=args.windows_per_conversation,
        size=settings.extract_window_size, step=settings.extract_window_step,
    )
    if not windows:
        print("no windows found; is the database reachable and populated?", file=sys.stderr)
        return 1
    if args.repeat > 1:
        windows = windows * args.repeat

    prompts = [
        build_extraction_prompt(window)
        for _, window in windows
    ]
    message_counts = [len(window) for _, window in windows]

    print(json.dumps({
        "model": settings.extract_model,
        "disable_thinking": settings.extract_disable_thinking,
        "max_tokens": settings.extract_max_tokens,
        "window_size": settings.extract_window_size,
        "window_step": settings.extract_window_step,
        "window_count": len(prompts),
        "messages_per_window": {
            "min": min(message_counts), "max": max(message_counts),
            "avg": round(sum(message_counts) / len(message_counts), 1),
        },
        "prompt_chars": {
            "min": min(len(p) for p in prompts),
            "max": max(len(p) for p in prompts),
        },
    }, ensure_ascii=False))

    results: dict[str, dict] = {}
    for level in (int(value) for value in str(args.concurrency).split(",") if value.strip()):
        client = _RecordingClient()
        started = time.perf_counter()
        failures = 0
        if level <= 1:
            for prompt in prompts:
                try:
                    client.extract(prompt)
                except ExtractionError:
                    failures += 1
        else:
            with ThreadPoolExecutor(max_workers=level) as pool:
                futures = [pool.submit(client.extract, prompt) for prompt in prompts]
                for future in futures:
                    try:
                        future.result()
                    except ExtractionError:
                        failures += 1
        wall = time.perf_counter() - started
        latencies = [item["latency_ms"] for item in client.calls]
        ok = [item for item in client.calls if item["ok"]]
        total_tokens = sum(item.get("total_tokens", 0) for item in client.calls)
        results[str(level)] = {
            "wall_seconds": round(wall, 2),
            "calls": len(client.calls),
            "failures": failures,
            "latency_ms": {
                "p50": _percentile(latencies, 0.5),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies) if latencies else 0,
            },
            "tokens": {
                "total": total_tokens,
                "avg_per_call": round(total_tokens / len(client.calls)) if client.calls else 0,
                "prompt": sum(item.get("prompt_tokens", 0) for item in ok),
                "completion": sum(item.get("completion_tokens", 0) for item in ok),
            },
            # tokens/second scaled to a minute, which is the ceiling the provider
            # has to sustain for the scan to keep up with arrival rate.
            "tokens_per_minute": round(total_tokens / wall * 60) if wall > 0 else 0,
            "entities_found": sum(item.get("entities", 0) for item in ok),
            "relations_found": sum(item.get("relations", 0) for item in ok),
            # Per-window counts, so an "average" cannot hide windows that came
            # back empty for no apparent reason.
            "per_window_entities": [item.get("entities", 0) for item in ok],
            "empty_windows": sum(1 for item in ok if not item.get("entities")),
        }
        print(json.dumps({"concurrency": level, **results[str(level)]}, ensure_ascii=False))

    repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
