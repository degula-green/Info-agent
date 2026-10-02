"""Locate and load the shared intent contract used by the Laya fine-tuning.

The contract lives inside the Agent package so the running service has it
next to the code that validates model output. Training and dataset generation
read the same file; ``services/agent/tests/test_intent_contract.py`` asserts
that both sides agree on names, order, criteria and task kinds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONTRACT_RELATIVE = Path("services/agent/app/understanding/intent_contract.json")


def find_contract_path() -> Path:
    override = os.environ.get("INTENT_CONTRACT_PATH", "").strip()
    if override:
        path = Path(override)
        if not path.exists():
            raise RuntimeError(f"INTENT_CONTRACT_PATH does not exist: {path}")
        return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CONTRACT_RELATIVE
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "intent contract not found; set INTENT_CONTRACT_PATH to "
        "services/agent/app/understanding/intent_contract.json"
    )


def load_contract() -> dict[str, Any]:
    path = find_contract_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    options = raw.get("options")
    if not isinstance(options, list) or not options:
        raise RuntimeError(f"intent contract has no options: {path}")
    return raw


def option_order(contract: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(option["name"]) for option in contract["options"])


def criteria(contract: dict[str, Any]) -> dict[str, str]:
    return {
        str(option["name"]): str(option["laya_criteria"])
        for option in contract["options"]
    }
