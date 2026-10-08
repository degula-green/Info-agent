"""Windows sidecar entry point used by the desktop build."""

from __future__ import annotations

import uvicorn

from app.config import load_settings


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
