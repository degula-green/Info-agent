#!/usr/bin/env python3
"""Synchronize the local deletion model additions to an OpenFGA store.

The repository model already defines `organization#information_admin` and
`knowledge_item#moderator/delete`. Older deployed stores may still reference a
model that predates those relations, which makes Core return
`AUTHZ_BACKEND_UNAVAILABLE` for deletion checks.

This script reads CORE_OPENFGA_URL/STORE_ID/MODEL_ID from services/core/.env,
creates a new model based on the store's current model, adds the deletion
relations when missing, and updates CORE_OPENFGA_MODEL_ID in place.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def request_json(method: str, url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenFGA {method} {url} failed: {exc.code} {exc.read().decode(errors='replace')}") from exc


def add_user_relation(type_definition: dict, relation: str) -> None:
    type_definition["relations"][relation] = {"this": {}}
    type_definition["metadata"]["relations"][relation] = {
        "directly_related_user_types": [{"type": "user", "condition": ""}],
        "module": "",
        "source_info": None,
    }


def add_computed_relation(type_definition: dict, relation: str, definition: dict) -> None:
    type_definition["relations"][relation] = definition
    type_definition["metadata"]["relations"][relation] = {
        "directly_related_user_types": [],
        "module": "",
        "source_info": None,
    }


def patch_model(model: dict) -> bool:
    changed = False
    for type_definition in model["type_definitions"]:
        if type_definition["type"] == "organization":
            if "information_admin" not in type_definition["relations"]:
                add_user_relation(type_definition, "information_admin")
                changed = True
        if type_definition["type"] == "knowledge_item":
            if "moderator" not in type_definition["relations"]:
                add_computed_relation(
                    type_definition,
                    "moderator",
                    {
                        "tupleToUserset": {
                            "tupleset": {"object": "", "relation": "organization"},
                            "computedUserset": {"object": "", "relation": "information_admin"},
                        }
                    },
                )
                changed = True
            if "delete" not in type_definition["relations"]:
                add_computed_relation(
                    type_definition,
                    "delete",
                    {
                        "union": {
                            "child": [
                                {"computedUserset": {"object": "", "relation": "owner"}},
                                {"computedUserset": {"object": "", "relation": "moderator"}},
                            ]
                        }
                    },
                )
                changed = True
    return changed


def update_model_id(env_path: Path, model_id: str) -> None:
    lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    updated = False
    result: list[str] = []
    for line in lines:
        stripped = line.lstrip("#").strip()
        if stripped.startswith("CORE_OPENFGA_MODEL_ID="):
            result.append(f"CORE_OPENFGA_MODEL_ID={model_id}")
            updated = True
        else:
            result.append(line)
    if not updated:
        result.append(f"CORE_OPENFGA_MODEL_ID={model_id}")
    env_path.write_text("\n".join(result) + "\n", encoding="utf-8")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = root / "services" / "core" / ".env"
    env = load_env(env_path)
    base_url = env.get("CORE_OPENFGA_URL", "").rstrip("/")
    store_id = env.get("CORE_OPENFGA_STORE_ID", "")
    if not base_url or not store_id:
        raise RuntimeError("CORE_OPENFGA_URL and CORE_OPENFGA_STORE_ID are required")

    store_url = f"{base_url}/stores/{store_id}"
    models = request_json("GET", f"{store_url}/authorization-models").get("authorization_models", [])
    if not models:
        raise RuntimeError("OpenFGA store has no authorization model")
    # OpenFGA returns authorization models newest-first. Taking the last item
    # can select the oldest legacy model and cause the script to recreate the
    # same patch on every run.
    model = models[0]
    if not patch_model(model):
        print(f"OpenFGA model already current: {model['id']}")
        update_model_id(env_path, model["id"])
        return 0

    payload = {
        "schema_version": "1.1",
        "type_definitions": model["type_definitions"],
        "conditions": model.get("conditions", {}),
    }
    created = request_json("POST", f"{store_url}/authorization-models", payload)
    model_id = created.get("authorization_model_id", "")
    if not model_id:
        raise RuntimeError(f"OpenFGA did not return authorization_model_id: {created}")
    update_model_id(env_path, model_id)
    print(f"OpenFGA model updated: {model_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"sync-openfga-model failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
