from __future__ import annotations

from app.infrastructure.http import join_url


def knowledge_api_url(base_url: str, path: str) -> str:
    return join_url((base_url or "").rstrip("/"), path)
