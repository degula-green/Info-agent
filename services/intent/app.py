"""Serve only the standalone multilingual checkpoint with an optional pin."""

from __future__ import annotations

import os

import uvicorn
from laya import Router
from laya.serve import create_app


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MODEL = "multilingual"
REPO = "convaiinnovations/laya-multilingual"
MODEL_PATH = os.environ.get("LAYA_MODEL_PATH", "").strip()
DEVICE_VALUE = os.environ.get("LAYA_DEVICE", "").strip()
DEVICE = None if not DEVICE_VALUE or DEVICE_VALUE.lower() == "auto" else DEVICE_VALUE


def _verify_model_contract() -> None:
    """Refuse to serve a checkpoint that was trained for another intent schema.

    Opt-in through ``LAYA_REQUIRE_INTENT_CONTRACT`` so the stock multilingual
    checkpoint can still be served while a domain checkpoint is being built.
    """

    if not MODEL_PATH or not _env_bool("LAYA_REQUIRE_INTENT_CONTRACT", False):
        return
    import json
    from pathlib import Path

    from intent_contract import load_contract, option_order

    contract = load_contract()
    config_path = Path(MODEL_PATH) / "rl_agent_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    version = config.get("intent_schema_version")
    expected_version = str(contract["schema_version"])
    if version != expected_version:
        raise RuntimeError(
            f"LAYA_MODEL_PATH {MODEL_PATH} was trained for schema {version!r}, "
            f"but the contract is {expected_version!r}"
        )
    if tuple(config.get("option_order") or ()) != option_order(contract):
        raise RuntimeError(
            f"LAYA_MODEL_PATH {MODEL_PATH} option_order does not match the contract"
        )


_verify_model_contract()

router = Router(
    models={MODEL: (MODEL_PATH or REPO, None)},
    revisions={
        MODEL: (
            None
            if MODEL_PATH
            else os.environ.get("LAYA_REVISION", "").strip() or None
        ),
    },
    device=DEVICE,
)
if _env_bool("LAYA_PRELOAD", True):
    router.preload([MODEL])

app = create_app(router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("LAYA_HOST", "0.0.0.0"),
        port=int(os.environ.get("LAYA_PORT", "8110")),
        log_level=os.environ.get("LAYA_LOG_LEVEL", "info"),
    )
