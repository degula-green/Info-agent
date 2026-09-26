from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.application.mvp_ports import AuthorizationGateway
from app.config import settings
from app.domain.rag import AccessCheck, AuthorizationScope
from app.infrastructure.http import HttpClient, IntegrationError, join_url, with_query


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: AuthorizationScope


class RagAuthorizationClient(AuthorizationGateway):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.authz_base_url).rstrip("/")
        self.token = token if token is not None else settings.authz_api_token
        self.http = http or HttpClient()
        self._cache: dict[tuple[str, str, str, tuple[str, ...]], _CacheEntry] = {}
        self._lock = threading.Lock()

    def search_scope(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        resource_parts: tuple[str, ...],
    ) -> AuthorizationScope:
        key = (user_id, scope_type, scope_id, tuple(sorted(resource_parts)))
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return cached.value
        if not self.base_url:
            return AuthorizationScope(scope_type, scope_id, available=False)
        request_id = uuid.uuid4().hex
        try:
            response = self.http.request(
                "POST",
                join_url(self.base_url, "/internal/v1/authorization/search-scope"),
                body={
                    "subject_type": "user",
                    "subject_id": user_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "resource_parts": list(resource_parts),
                },
                token=self.token,
                headers={
                    "X-Caller-Service": "rag",
                    "X-Request-ID": request_id,
                    "X-Trace-ID": request_id,
                },
                timeout=settings.authz_timeout_seconds,
            ).json()
            value = _scope_from_response(response, scope_type=scope_type, scope_id=scope_id)
        except IntegrationError:
            value = AuthorizationScope(scope_type, scope_id, available=False)
        ttl = min(5, max(0, settings.authz_scope_cache_ttl_seconds))
        with self._lock:
            self._cache[key] = _CacheEntry(now + ttl, value)
        return value

    def check_batch(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        checks: list[AccessCheck],
        snapshot_id: str | None = None,
    ) -> list[bool]:
        if not checks:
            return []
        if not self.base_url:
            return [False] * len(checks)
        decisions: list[bool] = []
        for start in range(0, len(checks), 100):
            batch = checks[start:start + 100]
            request_id = uuid.uuid4().hex
            try:
                response = self.http.request(
                    "POST",
                    join_url(self.base_url, "/internal/v1/authorization/check-batch"),
                    body={
                        "subject_type": "user",
                        "subject_id": user_id,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "snapshot_id": snapshot_id,
                        "checks": [
                            {
                                "check_id": f"c{index}",
                                "resource_type": item.resource_type,
                                "resource_part": item.resource_part,
                                "resource_id": item.resource_id,
                                "action": item.action,
                            }
                            for index, item in enumerate(batch, start=1)
                        ],
                    },
                    token=self.token,
                    headers={
                        "X-Caller-Service": "rag",
                        "X-Request-ID": request_id,
                        "X-Trace-ID": request_id,
                    },
                    timeout=settings.authz_timeout_seconds,
                ).json()
                values = response.get("decisions", []) if isinstance(response, dict) else []
                by_id = {
                    str(item.get("check_id")): bool(item.get("allowed"))
                    for item in values
                    if isinstance(item, dict)
                }
                expected = [f"c{index}" for index in range(1, len(batch) + 1)]
                if any(key not in by_id for key in expected) or len(by_id) != len(expected):
                    decisions.extend([False] * len(batch))
                else:
                    decisions.extend(by_id[key] for key in expected)
            except Exception:
                decisions.extend([False] * len(batch))
        return decisions if len(decisions) == len(checks) else [False] * len(checks)

    def check_organization_capability(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        capability: str,
    ) -> bool:
        if scope_type != "organization" or not self.base_url:
            return False
        request_id = uuid.uuid4().hex
        try:
            response = self.http.request(
                "GET",
                with_query(
                    join_url(
                        self.base_url,
                        f"/internal/organizations/{scope_id}/members/{user_id}/check",
                    ),
                    capability=capability,
                ),
                token=self.token,
                headers={
                    "X-Caller-Service": "rag",
                    "X-Request-ID": request_id,
                    "X-Trace-ID": request_id,
                },
                timeout=settings.authz_timeout_seconds,
            ).json()
            return bool(response.get("allowed")) if isinstance(response, dict) else False
        except IntegrationError:
            return False


class AllowAllAuthorizationGateway(AuthorizationGateway):
    """Development-only display gateway; protected scope remains unavailable."""

    def search_scope(self, *, scope_type: str, scope_id: str, **_: Any) -> AuthorizationScope:
        return AuthorizationScope(scope_type, scope_id, available=False)

    def check_batch(self, *, checks: list[AccessCheck], **_: Any) -> list[bool]:
        return [True] * len(checks)

    def check_organization_capability(self, **_: Any) -> bool:
        return True


def _scope_from_response(
    value: Any,
    *,
    scope_type: str,
    scope_id: str,
) -> AuthorizationScope:
    if not isinstance(value, dict):
        return AuthorizationScope(scope_type, scope_id, available=False)
    truncated = bool(value.get("truncated", False))
    return AuthorizationScope(
        scope_type=scope_type,
        scope_id=scope_id,
        snapshot_id=_text(value.get("snapshot_id")),
        expires_at=_text(value.get("expires_at")),
        authorized_organization_ids=tuple(str(item) for item in value.get("authorized_organization_ids") or []),
        authorized_conversation_group_ids=tuple(str(item) for item in value.get("authorized_conversation_group_ids") or []),
        authorized_protected_object_keys=tuple(
            str(item) for item in value.get("authorized_protected_object_keys") or []
        ),
        available=bool(value.get("available", True)),
        truncated=truncated,
    )


def _text(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value)
