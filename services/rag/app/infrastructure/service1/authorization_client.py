from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.application.ports import AuthorizationGateway
from app.config import settings
from app.domain.models import AccessCheck, AuthorizationScope
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class AuthorizationUnavailable(RuntimeError):
    """Raised only when a caller explicitly requires an authorization decision."""


@dataclass(frozen=True)
class _ScopeCacheEntry:
    expires_at: float
    value: AuthorizationScope


class Service1AuthorizationClient(AuthorizationGateway):
    """Call service 1; OpenFGA remains entirely inside service 1."""

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
        self._cache: dict[tuple[Any, ...], _ScopeCacheEntry] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def search_scope(
        self,
        *,
        user_id: str,
        organization_id: str | None,
        resource_parts: tuple[str, ...],
        knowledge_base_id: str | None = None,
    ) -> AuthorizationScope:
        key = (user_id, organization_id, tuple(sorted(resource_parts)), knowledge_base_id)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return cached.value
            if cached:
                self._cache.pop(key, None)
        if not self.configured:
            return AuthorizationScope(available=False)
        payload = {
            "subject_type": "user",
            "subject_id": str(user_id),
            "organization_id": organization_id,
            "resource_parts": list(resource_parts),
            "knowledge_base_id": knowledge_base_id,
        }
        try:
            response = self.http.request(
                "POST",
                join_url(self.base_url, "/internal/v1/authorization/search-scope"),
                body=payload,
                headers=_integration_headers(),
                token=self.token,
                timeout=settings.authz_timeout_seconds,
            ).json()
            value = _scope_from_response(response)
        except IntegrationError:
            # Protected search must fail closed. Returning unavailable lets the
            # application omit that branch and record the degradation.
            value = AuthorizationScope(available=False)
        if len(value.object_keys) > settings.authz_scope_max_objects:
            return AuthorizationScope(
                snapshot_id=value.snapshot_id,
                expires_at=value.expires_at,
                objects={},
                available=False,
            )
        with self._lock:
            self._cache[key] = _ScopeCacheEntry(
                now + max(0, settings.authz_scope_cache_ttl_seconds), value
            )
        return value

    def check_batch(
        self,
        *,
        user_id: str,
        organization_id: str | None,
        checks: list[AccessCheck],
        snapshot_id: str | None = None,
    ) -> list[bool]:
        if not checks:
            return []
        if not self.configured:
            return [False] * len(checks)
        payload = {
            "subject_type": "user",
            "subject_id": str(user_id),
            "organization_id": organization_id,
            "snapshot_id": snapshot_id,
            "checks": [
                {
                    "check_id": f"c{index}",
                    "resource_type": item.resource_type,
                    "resource_part": item.resource_part,
                    "resource_id": item.resource_id,
                    "action": item.action,
                }
                for index, item in enumerate(checks, start=1)
            ],
        }
        try:
            response = self.http.request(
                "POST",
                join_url(self.base_url, "/internal/v1/authorization/check-batch"),
                body=payload,
                headers=_integration_headers(),
                token=self.token,
                timeout=settings.authz_timeout_seconds,
            ).json()
            decisions = response.get("decisions", response.get("results", [])) if isinstance(response, dict) else []
            if isinstance(decisions, list) and all(isinstance(item, dict) and "check_id" in item for item in decisions):
                by_id = {str(item.get("check_id")): bool(item.get("allowed", False)) for item in decisions}
                expected = [f"c{index}" for index in range(1, len(checks) + 1)]
                if any(check_id not in by_id for check_id in expected) or len(by_id) != len(expected):
                    return [False] * len(checks)
                return [by_id[check_id] for check_id in expected]
            allowed: list[bool] = []
            for item in decisions:
                if isinstance(item, bool):
                    allowed.append(item)
                elif isinstance(item, dict):
                    allowed.append(bool(item.get("allowed", False)))
                else:
                    allowed.append(False)
            return allowed if len(allowed) == len(checks) else [False] * len(checks)
        except IntegrationError:
            return [False] * len(checks)


class AllowAllAuthorizationGateway(AuthorizationGateway):
    """Explicit test/dev double; never selected by production bootstrap."""

    def search_scope(self, **_: Any) -> AuthorizationScope:
        return AuthorizationScope(objects={"knowledge_original": ("*",), "attachment_content": ("*",)})

    def check_batch(self, *, checks: list[AccessCheck], **_: Any) -> list[bool]:
        return [True] * len(checks)


def _scope_from_response(value: Any) -> AuthorizationScope:
    if not isinstance(value, dict):
        return AuthorizationScope(available=False)
    objects = value.get("objects") or value.get("authorized_objects") or {}
    normalized: dict[str, tuple[str, ...]] = {}
    if bool(value.get("truncated", False)):
        return AuthorizationScope(available=False)
    if isinstance(objects, dict):
        for name, raw_values in objects.items():
            if isinstance(raw_values, list):
                normalized[str(name)] = tuple(str(item) for item in raw_values if item)
    elif isinstance(objects, list):
        keys = tuple(str(item) for item in objects if item)
        normalized["protected"] = keys
    all_keys = tuple(key for values in normalized.values() for key in values)
    allowed_prefixes = ("knowledge_original:", "attachment_content:")
    if any(key == "*" or not key.startswith(allowed_prefixes) for key in all_keys):
        return AuthorizationScope(available=False)
    return AuthorizationScope(
        snapshot_id=_optional(value.get("snapshot_id")),
        expires_at=_optional(value.get("expires_at")),
        objects=normalized,
        available=bool(value.get("available", True)),
    )


def _optional(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _integration_headers() -> dict[str, str]:
    # The caller identity is bound to the service token; request/trace IDs are
    # generated here when the application has not supplied a request context.
    request_id = uuid.uuid4().hex
    return {
        "X-Caller-Service": settings.authz_caller_service,
        "X-Request-ID": request_id,
        "X-Trace-ID": request_id,
    }
