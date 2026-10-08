"""Knowledge-backed authentication for desktop device requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

import httpx


class DeviceAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    owner_user_id: str
    connector_id: str
    expires_at: str


class DeviceAuthClient(Protocol):
    def get_active_device(self, owner_user_id: str) -> DeviceIdentity:
        ...

    def verify_request(
        self,
        *,
        device_id: str,
        headers: Mapping[str, str],
        method: str,
        path: str,
        payload_hash: str,
    ) -> DeviceIdentity:
        ...


class HttpDeviceAuthClient:
    def __init__(
        self,
        base_url: str,
        *,
        service_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.service_token = str(service_token or "").strip()
        self.timeout_seconds = float(timeout_seconds)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.service_token:
            headers["X-Service-Token"] = self.service_token
        return headers

    def get_active_device(self, owner_user_id: str) -> DeviceIdentity:
        if not self.base_url:
            raise DeviceAuthError("knowledge device service is not configured")
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get(
                    "/api/knowledge/v1/internal/devices/active",
                    params={"owner_user_id": owner_user_id},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise DeviceAuthError(
                f"knowledge device lookup failed: {exc}", status_code=503
            ) from exc
        if response.status_code >= 400:
            raise DeviceAuthError(
                self._error_message(response),
                status_code=response.status_code,
            )
        payload = response.json()
        return DeviceIdentity(
            device_id=str(payload.get("device_id") or ""),
            owner_user_id=str(payload.get("owner_user_id") or ""),
            connector_id=str(payload.get("connector_id") or ""),
            expires_at=str(payload.get("expires_at") or ""),
        )

    def verify_request(
        self,
        *,
        device_id: str,
        headers: Mapping[str, str],
        method: str,
        path: str,
        payload_hash: str,
    ) -> DeviceIdentity:
        if not self.base_url:
            raise DeviceAuthError("knowledge device service is not configured")
        forwarded = self._headers()
        for name in (
            "X-Agent-Device-Key",
            "X-Agent-Timestamp",
            "X-Agent-Signature",
        ):
            if name in headers:
                forwarded[name] = headers[name]
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.post(
                    f"/api/knowledge/v1/internal/devices/{device_id}/verify-request",
                    headers=forwarded,
                    json={
                        "original_method": method,
                        "original_path": path,
                        "payload_hash": payload_hash,
                    },
                )
        except httpx.HTTPError as exc:
            raise DeviceAuthError(
                f"knowledge device verification failed: {exc}", status_code=503
            ) from exc
        if response.status_code >= 400:
            raise DeviceAuthError(
                self._error_message(response),
                status_code=response.status_code,
            )
        payload = response.json()
        return DeviceIdentity(
            device_id=str(payload.get("device_id") or ""),
            owner_user_id=str(payload.get("owner_user_id") or ""),
            connector_id=str(payload.get("connector_id") or ""),
            expires_at=str(payload.get("expires_at") or ""),
        )

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            return str(
                payload.get("message")
                or payload.get("detail")
                or payload.get("code")
                or f"device request rejected ({response.status_code})"
            )
        return f"device request rejected ({response.status_code})"
