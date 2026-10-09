"""Measure how often a window comes back empty, and whether retrying helps.

    python scripts/probe_extraction_stability.py --windows 6 --rounds 3 --concurrency 6

The window scan was measured returning nothing for ~2/3 of windows, with the
empty set differing between runs. This probe separates two explanations:

* the window genuinely has no entities, or
* the model skipped it this time

It does that by running each window several times and reporting, per window, how
many attempts produced entities. A window that succeeds sometimes and fails
others is instability, not an empty window.

It also measures the cheapest mitigation: retry once when a window comes back
empty, and how many extra calls that costs.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.window_scan_service import (  # noqa: E402
    build_extraction_prompt,
    build_windows,
    clean_entities,
)
from app.config import settings  # noqa: E402
from app.infrastructure.extraction.client import EntityExtractionClient  # noqa: E402
from app.infrastructure.persistence.mvp import PostgresRagMVPRepository  # noqa: E402


def _count_entities(payload) -> int:
    return len(clean_entities(payload.get("entities")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--window-size", type=int, default=settings.extract_window_size)
    parser.add_argument("--out", default=None, help="write the full report to this path")
    parser.add_argument("--model", default=None,
                        help="override the extraction model (defaults to RAG_EXTRACT_MODEL)")
    args = parser.parse_args(argv)

    repository = PostgresRagMVPRepository()
    prompts: list[str] = []
    for conversation in repository.list_active_conversations(limit=3):
        chunks = repository.list_conversation_chunks(
            scope_type=conversation["scope_type"], scope_id=conversation["scope_id"],
            conversation_id=conversation["conversation_id"],
            limit=args.window_size * 2,
        )
        if not chunks:
            continue
        for window in build_windows(chunks, args.window_size, max(1, args.window_size // 2)):
            prompts.append(build_extraction_prompt(window))
    prompts = prompts[: args.windows]
    if not prompts:
        print("no windows found", file=sys.stderr)
        return 1

    client = EntityExtractionClient(model=args.model) if args.model else EntityExtractionClient()

    # attempts[window][round] = entity count on the first call
    first_attempt: list[list[int]] = [[] for _ in prompts]
    retried_extra = 0
    recovered = 0
    still_empty = 0

    def run_once(prompt: str) -> int:
        return _count_entities(client.extract(prompt))

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for _round in range(args.rounds):
            counts = list(pool.map(run_once, prompts))
            for index, count in enumerate(counts):
                first_attempt[index].append(count)
            # Retry the empties once, which is what the worker would do.
            empty_indexes = [i for i, value in enumerate(counts) if value == 0]
            if empty_indexes:
                retried_extra += len(empty_indexes)
                retries = list(pool.map(run_once, [prompts[i] for i in empty_indexes]))
                for index, value in zip(empty_indexes, retries):
                    if value:
                        recovered += 1
                    else:
                        still_empty += 1

    per_window = [
        {
            "window": index,
            "attempts": values,
            "successes": sum(1 for value in values if value),
            "verdict": (
                "unstable" if 0 < sum(1 for value in values if value) < len(values)
                else "always_empty" if not any(values)
                else "always_found"
            ),
        }
        for index, values in enumerate(first_attempt)
    ]
    total_attempts = len(prompts) * args.rounds
    empty_first = sum(1 for values in first_attempt for value in values if value == 0)
    report = {
        "windows": len(prompts),
        "rounds": args.rounds,
        "window_size": args.window_size,
        "first_attempt_empty_rate": round(empty_first / total_attempts, 4) if total_attempts else 0.0,
        "retry_calls": retried_extra,
        "retry_recovered": recovered,
        "retry_still_empty": still_empty,
        "retry_recovery_rate": round(recovered / retried_extra, 4) if retried_extra else 0.0,
        "extra_call_ratio": round(retried_extra / total_attempts, 4) if total_attempts else 0.0,
        "per_window": per_window,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "per_window"},
                         ensure_ascii=False))
    else:
        print(text)
    repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
