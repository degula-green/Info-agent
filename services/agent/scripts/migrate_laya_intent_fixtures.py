"""Migrate legacy JSONL intent fixtures to the intent-v6 contract.

``form.prepare`` becomes ``form.complete`` and ``document.compare`` becomes an
``other_task`` boundary sample. Legacy ``form.submit`` rows are never mapped
automatically: the text of each row must be reviewed, because "submit" can mean
"write the confirmed values into the form" (``form.complete``) or "send the
filled form to an external platform" (``other_task``).

With ``--form-submit-target review`` (the default) the script only prints the
affected rows and exits non-zero. Pass ``other_task`` or ``form_complete`` to
record the human decision and rewrite the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.understanding.schema import (  # noqa: E402
    ALL_INTENT_LABELS,
    INTENT_OPTION_ORDER,
    INTENT_SCHEMA_VERSION,
)

LEGACY_MIGRATION = {
    "form.prepare": "form.complete",
    "document.compare": "other_task",
}
LEGACY_FORM_SUBMIT = "form.submit"


def migrate(rows: list[dict], form_submit_target: str) -> tuple[list[dict], list[str]]:
    reviewed: list[str] = []
    migrated: list[dict] = []
    for row in rows:
        intent = str(row.get("intent", ""))
        if intent == LEGACY_FORM_SUBMIT:
            reviewed.append(str(row.get("text")))
            intent = form_submit_target
        else:
            intent = LEGACY_MIGRATION.get(intent, intent)
        if intent not in ALL_INTENT_LABELS:
            raise ValueError(
                f"unknown intent {intent!r} in {row.get('text')!r}; "
                f"{INTENT_SCHEMA_VERSION} allows: {', '.join(INTENT_OPTION_ORDER)}"
            )
        migrated.append({**row, "intent": intent})
    return migrated, reviewed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument(
        "--form-submit-target",
        choices=["review", "other_task", "form.complete"],
        default="review",
    )
    args = parser.parse_args()

    for path in args.paths:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if args.form_submit_target == "review":
            pending = [row for row in rows if row.get("intent") == LEGACY_FORM_SUBMIT]
            if pending:
                print(f"{path}: {len(pending)} form.submit rows need review")
                for row in pending:
                    print(f"  - {row.get('text')}")
        migrated, reviewed = migrate(rows, args.form_submit_target)
        if args.form_submit_target == "review":
            continue
        path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in migrated
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {"path": str(path), "rows": len(migrated), "reviewed_form_submit": len(reviewed)},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
