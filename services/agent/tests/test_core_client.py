from __future__ import annotations

from app.infrastructure.core.client import HttpCoreClient
from app.infrastructure.http import HttpResult


class _HTTP:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return HttpResult(
            status=200,
            headers={},
            body=b'{"organization":{"id":"org-1"},"membership":{"status":"active"}}',
        )


def test_http_core_client_resolves_current_organization_with_user_token() -> None:
    http = _HTTP()
    client = HttpCoreClient(base_url="http://core", http=http)
    assert client.current_organization("user-jwt") == "org-1"
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"] == "http://core/organizations/current"
    assert http.calls[0]["token"] == "user-jwt"
