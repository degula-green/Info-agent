"""Public-page fetching behind web.fetch.

Only public, read-only pages: the URL is validated before it is opened and
again after every redirect, because a redirect is the cheapest way to smuggle
a private address past a single check. Responses are capped in size and
restricted to text content types; anything else is a permanent error rather
than a silently empty page.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict

ALLOWED_SCHEMES = frozenset({"http", "https"})
# Documentation sites increasingly publish a markdown twin of each page and
# advertise it as the better version for a model to read. It is still plain
# text to this service, so refusing it only pushed the Planner into a re-plan.
ALLOWED_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}
)
DEFAULT_USER_AGENT = "info-agent/0.1 (+web.fetch)"


class PageFetchError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "page_fetch_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class PageFetchRetryable(PageFetchError):
    """Transient failure: the same URL may work on the next attempt."""

    classification = "retryable_error"
    code = "page_fetch_retryable"


class PageDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    status: int
    content_type: str
    content: str
    truncated: bool = False
    fetched_at: datetime


class PageFetcher(Protocol):
    def fetch(self, url: str, *, max_bytes: int | None = None) -> PageDocument:
        ...


def assert_public_url(url: str, *, allow_private_addresses: bool = False) -> str:
    """Returns the URL, or raises when it must not be fetched.

    Called for the requested URL and again for every redirect target: a host
    that passes here can still be redirected to 127.0.0.1 by the remote side.
    ``allow_private_addresses`` is the operator switch for environments whose
    resolver hands out non-public addresses on purpose (fake-IP proxies); it
    only skips the address check, never the scheme or credential rules.
    """

    candidate = str(url or "").strip()
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise PageFetchError(
            f"unsupported URL scheme: {parsed.scheme or '(none)'}",
            code="unsupported_scheme",
        )
    if not parsed.hostname:
        raise PageFetchError("URL has no host", code="invalid_url")
    if parsed.username or parsed.password:
        raise PageFetchError("URL must not carry credentials", code="invalid_url")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    _assert_public_host(parsed.hostname, port, allow_private=allow_private_addresses)
    return candidate


def _assert_public_host(host: str, port: int, *, allow_private: bool = False) -> None:
    """A literal address is checked directly; a name is checked after DNS.

    Every resolved address must be public, not just the first one: a host that
    answers with one public and one private address must not be reachable.
    """

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not allow_private:
            _assert_public_address(literal, host)
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        if allow_private:
            # The operator explicitly allowed non-public addresses (fake-IP
            # proxies commonly answer only the application's own resolver).
            # Let the fetch report the real failure instead of blocking it
            # here with a guard that is meant for the public-address rule.
            return
        raise PageFetchRetryable(f"cannot resolve host: {host}") from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise PageFetchRetryable(f"cannot resolve host: {host}")
    if allow_private:
        return
    for address in addresses:
        _assert_public_address(ipaddress.ip_address(address), host)


def _assert_public_address(address, host: str) -> None:
    """Rejects anything that is not a global address (loopback, private, ...)."""

    resolved = address
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        resolved = address.ipv4_mapped
    if not resolved.is_global or resolved.is_multicast or resolved.is_reserved:
        raise PageFetchError(
            f"refusing non-public address for {host}: {address}",
            code="blocked_address",
        )


def _read_limited(response, limit: int) -> tuple[bytes, bool]:
    """Reads at most ``limit`` bytes and reports whether more remained."""

    limit = max(int(limit), 1)
    data = response.read(limit + 1)
    if len(data) > limit:
        return data[:limit], True
    return data, False


def _decode_body(raw: bytes, content_encoding: str, limit: int) -> tuple[bytes, bool]:
    """Undo the compression a server applied even when identity was requested.

    CDNs gzip regardless of Accept-Encoding, and urllib does not decompress.
    Feeding those bytes to a text decoder produces mojibake that reads like a
    page of binary noise -- it fooled a real planner run into a pointless
    re-plan -- so the body is decoded here rather than downstream.
    """

    encoding = str(content_encoding or "").split(",")[0].strip().lower()
    if encoding in ("", "identity"):
        return raw, False
    if encoding in ("gzip", "x-gzip"):
        try:
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        except zlib.error as exc:
            raise PageFetchError(
                f"cannot decode gzip response: {exc}", code="bad_content_encoding"
            ) from exc
    elif encoding == "deflate":
        try:
            data = zlib.decompressobj(zlib.MAX_WBITS).decompress(raw)
        except zlib.error:
            try:
                data = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)
            except zlib.error as exc:
                raise PageFetchError(
                    f"cannot decode deflate response: {exc}",
                    code="bad_content_encoding",
                ) from exc
    else:
        raise PageFetchError(
            f"unsupported content encoding: {encoding}",
            code="unsupported_content_encoding",
        )
    if len(data) > limit:
        return data[:limit], True
    return data, False


def _charset(content_type: str | None) -> str:
    for part in str(content_type or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value.strip():
            return value.strip().strip(chr(34))
    return "utf-8"


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validates every redirect hop before urllib follows it."""

    def __init__(
        self, max_redirections: int, *, allow_private_addresses: bool = False
    ) -> None:
        super().__init__()
        self.max_redirections = max(0, int(max_redirections))
        self.allow_private_addresses = allow_private_addresses

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl, allow_private_addresses=self.allow_private_addresses)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpPageFetcher:
    """stdlib-only fetcher: no new dependency, no login, no form posts."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_bytes: int = 512 * 1024,
        max_redirects: int = 3,
        allow_private_addresses: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self.allow_private_addresses = bool(allow_private_addresses)
        self.user_agent = user_agent

    def fetch(self, url: str, *, max_bytes: int | None = None) -> PageDocument:
        target = assert_public_url(
            url, allow_private_addresses=self.allow_private_addresses
        )
        limit = self.max_bytes if max_bytes is None else min(int(max_bytes), self.max_bytes)
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": self.user_agent,
                # Ask for an uncompressed body; servers that ignore this are
                # handled by _decode_body.
                "Accept-Encoding": "identity",
                "Accept": (
                    "text/html,application/xhtml+xml,text/markdown,"
                    "text/plain;q=0.9,*/*;q=0.1"
                ),
            },
            method="GET",
        )
        try:
            with self._open(request) as response:
                header = response.headers.get("Content-Type")
                content_type = str(header or "").split(";")[0].strip().lower()
                if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                    raise PageFetchError(
                        f"unsupported content type: {content_type}",
                        code="unsupported_content_type",
                    )
                final_url = response.geturl()
                assert_public_url(
                    final_url, allow_private_addresses=self.allow_private_addresses
                )
                raw, truncated = _read_limited(response, limit)
                body, decoded_truncated = _decode_body(
                    raw, response.headers.get("Content-Encoding") or "", limit
                )
                status = int(getattr(response, "status", 200) or 200)
                return PageDocument(
                    url=target,
                    final_url=final_url,
                    status=status,
                    content_type=content_type or "text/plain",
                    content=body.decode(_charset(header), errors="replace"),
                    truncated=truncated or decoded_truncated,
                    fetched_at=datetime.now(timezone.utc),
                )
        except PageFetchError:
            raise
        except UnicodeEncodeError as exc:
            # The request line goes out ASCII-encoded, so a URL carrying raw
            # non-ASCII characters cannot be sent at all. That is a bad URL, not
            # a transient failure worth three retries.
            raise PageFetchError(
                f"URL cannot be sent as ASCII: {exc}", code="invalid_url"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PageFetchRetryable(f"page fetch failed: {exc}") from exc

    def _open(self, request):
        """Opens the request; tests substitute this to avoid a live socket."""

        opener = urllib.request.build_opener(
            _ValidatingRedirectHandler(
                self.max_redirects,
                allow_private_addresses=self.allow_private_addresses,
            )
        )
        return opener.open(request, timeout=self.timeout_seconds)


def _http_error(exc: urllib.error.HTTPError) -> PageFetchError:
    """429 and 5xx are worth retrying; the rest will not change by asking again."""

    status = int(exc.code or 0)
    if status == 429 or status >= 500:
        error: PageFetchError = PageFetchRetryable(
            f"page fetch failed ({status})", code="http_error"
        )
    else:
        error = PageFetchError(f"page fetch rejected ({status})", code="http_error")
    # Carried on the error so the reader can tell "this page is gone" from
    # "this page refused a plain GET": only the latter is worth rendering.
    error.status = status
    return error
