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
