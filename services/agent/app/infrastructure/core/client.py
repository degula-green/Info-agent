from __future__ import annotations

from typing import Any, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url


class CoreClient(Protocol):
    def current_organization(self, access_token: str) -> str | None:
        ...

    def current_user_name(self, access_token: str) -> str | None:
        ...


class HttpCoreClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.http = http or HttpClient()

    def current_organization(self, access_token: str) -> str | None:
        token = str(access_token or "").strip()
        if not self.base_url or not token:
            return None
        try:
            result = self.http.request(
                "GET",
                join_url(self.base_url, "/organizations/current"),
                token=token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError:
            return None
        try:
            body: Any = result.json()
        except IntegrationError:
            return None
        if not isinstance(body, dict):
            return None
        organization = body.get("organization")
        if not isinstance(organization, dict):
            return None
        value = str(organization.get("id") or "").strip()
        return value or None

    def current_user_name(self, access_token: str) -> str | None:
        """The caller's own nickname.

        A report about the user themselves has to print a name, and the
        identity rows only carry generated account labels ("wxid_...",
        "用户12345"). The account profile is the one place that holds what the
        person actually calls themselves.
        """

        token = str(access_token or "").strip()
        if not self.base_url or not token:
            return None
        try:
            result = self.http.request(
                "GET",
                join_url(self.base_url, "/auth/me"),
                token=token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError:
            return None
        try:
            body: Any = result.json()
        except IntegrationError:
            return None
        if not isinstance(body, dict):
            return None
        return str(body.get("nickname") or "").strip() or None


class NullCoreClient:
    def current_organization(self, access_token: str) -> str | None:  # noqa: ARG002
        return None

    def current_user_name(self, access_token: str) -> str | None:  # noqa: ARG002
        return None
