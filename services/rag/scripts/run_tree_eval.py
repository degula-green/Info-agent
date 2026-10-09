"""Import and run a labelled evaluation set.

    # Load a frozen set (idempotent per suite + version + query)
    python scripts/run_tree_eval.py import \
        --suite entity_location --version 1 \
        --scope-type organization --scope-id <uuid> --file cases.json

    # Run it against the current code and record the metrics
    python scripts/run_tree_eval.py run \
        --suite entity_location --version 1 \
        --scope-type organization --scope-id <uuid> --label before-prompt-v2

The JSON file is a list of cases:

    [{"query": "A项目的服务器配置",
      "labels": {"entity_ids": ["<entity-uuid>"]},
      "notes": "简称场景"}]

For the retrieval suite the label key is "chunk_ids".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.entity_locator import EntityLocator  # noqa: E402
from app.application.bootstrap import build_container  # noqa: E402
from app.application.tree_eval_service import TreeEvalService  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("import", "run"))
    parser.add_argument("--suite", required=True,
                        choices=("entity_location", "retrieval", "mount"))
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--scope-type", default="organization", choices=("organization", "user"))
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--user-id", default=None,
                        help="required for the retrieval suite")
    parser.add_argument("--file", default=None, help="JSON file for the import command")
    parser.add_argument("--label", default=None, help="free-form tag recorded on the run")
    parser.add_argument("--k", type=int, default=10, help="cut-off for the retrieval suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Reuse the service container so the eval runs the same wiring production
    # does, including the embedding provider the locator depends on.
    container = build_container()
    locator = EntityLocator(repository=container.repository, embedding=container.embedding)
    service = TreeEvalService(
        repository=container.repository,
        locator=locator,
        retrieval=container.retrieval_service,
    )

    if args.command == "import":
        if not args.file:
            print("--file is required for the import command", file=sys.stderr)
            return 2
        cases = json.loads(Path(args.file).read_text(encoding="utf-8"))
        imported = service.import_cases(
            scope_type=args.scope_type, scope_id=args.scope_id, suite=args.suite,
            dataset_version=args.version, cases=cases,
        )
        print(json.dumps({"imported": imported, "suite": args.suite, "version": args.version},
                         ensure_ascii=False))
        return 0

    if args.suite == "entity_location":
        metrics = service.run_entity_location(
            scope_type=args.scope_type, scope_id=args.scope_id,
            dataset_version=args.version, label=args.label,
        )
    elif args.suite == "retrieval":
        if not args.user_id:
            print("--user-id is required for the retrieval suite", file=sys.stderr)
            return 2
        metrics = service.run_retrieval(
            scope_type=args.scope_type, scope_id=args.scope_id, user_id=args.user_id,
            dataset_version=args.version, k=args.k, label=args.label,
        )
    else:
        print("the mount suite is not implemented yet", file=sys.stderr)
        return 2
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
