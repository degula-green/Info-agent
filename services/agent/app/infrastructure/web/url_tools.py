"""URL and query handling for ``web.research``.

The Planner may propose URLs and domains, but nothing it proposes is trusted
until it can be tied back to the user's own words or to the reviewed alias
table. Those two rules live here so the capability, the search layer and the
tests share one meaning of "trusted".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# URLs are normally ASCII, but pages with unencoded Chinese paths are common.
# Allow those when the URL starts at a clear boundary. A URL glued to Chinese
# prose falls back to the ASCII form so a trailing question is not swallowed.
_UNICODE_URL_PATTERN = re.compile(
    r"""https?://[^\s<>"'()\[\]{}，。；：！？、（）【】《》]+""",
    re.IGNORECASE,
)
_ASCII_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+")
_TRAILING_JUNK = "。，、；：！？.,;:!?）)】]》\"'"
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = "=&%?:@!$'()*+,;/-._~"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", re.IGNORECASE)
DEFAULT_ALIASES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "web-search-aliases.json"
)


def extract_http_urls(text: str) -> list[str]:
    """Every http/https URL the user wrote, in order, without duplicates."""

    source = str(text or "")
    found: list[str] = []
    for match in _UNICODE_URL_PATTERN.finditer(source):
        previous = source[match.start() - 1] if match.start() else ""
        if previous and (previous.isalnum() or previous == "_"):
            ascii_match = _ASCII_URL_PATTERN.match(source, match.start())
            if ascii_match is None:
                continue
            url = ascii_match.group(0).rstrip(_TRAILING_JUNK)
        else:
            url = match.group(0).rstrip(_TRAILING_JUNK)
        if url and url not in found:
            found.append(url)
    return found


def normalize_url(url: str) -> str:
    """One canonical spelling per page, so deduplication actually dedupes.

    Host case, the default port and the fragment do not identify a different
    document; the query string does, so it is kept.
    """

    candidate = str(url or "").strip().rstrip(_TRAILING_JUNK)
    if candidate and "://" not in candidate:
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return candidate
    host = (parts.hostname or "").lower()
    if not host:
        return candidate
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    # Percent-encode what the request line cannot carry (a non-ASCII path is
    # legal in a URL, just not in an HTTP request line taken literally).
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    query = quote(parts.query, safe=_QUERY_SAFE)
    return urlunsplit((scheme, netloc, path, query, ""))


def unique_urls(urls: Iterable[str]) -> list[str]:
    """Normalized URLs in first-seen order."""

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        normalized = normalize_url(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def trusted_urls(proposed: Iterable[str], request: str) -> list[str]:
    """The user's own URLs, plus model-proposed ones the user actually wrote.

    A model that invents a plausible-looking link must not be able to send the
    Agent to it, so the request text is the only source of truth for what may
    be fetched.
    """

    detected = unique_urls(extract_http_urls(request))
    known = set(detected)
    accepted = [url for url in unique_urls(proposed) if url in known]
    return unique_urls(detected + accepted)


def domain_of(url: str) -> str:
    return (urlsplit(normalize_url(url)).hostname or "").lower()


def load_aliases(path: str | Path | None = None) -> dict[str, tuple[str, ...]]:
    """The reviewed platform-name table; a missing file means no aliases.

    A broken file is treated the same as a missing one: the alternative is
    failing every research task because an operator typo'd a JSON comma.
    """

    target = Path(path) if path else DEFAULT_ALIASES_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    aliases: dict[str, tuple[str, ...]] = {}
    for name, domains in dict(raw).items():
        cleaned = tuple(
            str(item).strip().lower() for item in domains if str(item).strip()
        )
        if cleaned:
            aliases[str(name).strip().lower()] = cleaned
    return aliases


def known_domains(aliases: Mapping[str, Sequence[str]] | None = None) -> set[str]:
    table = dict(aliases or load_aliases())
    return {domain for domains in table.values() for domain in domains}


def resolve_platform_domains(
    names: Iterable[str],
    *,
    request: str = "",
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Platform names to domains, rejecting anything the user did not imply.

    A model may name a domain directly, but only one the alias table knows or
    the user wrote themselves; otherwise a search could be steered to any site
    by the model alone.
    """

    table = dict(aliases or load_aliases())
    allowed = known_domains(table)
    request_text = str(request or "").lower()
    resolved: list[str] = []
    for raw in names:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        if value in table:
            candidates: Sequence[str] = table[value]
        elif "." in value and "/" not in value and " " not in value:
            candidates = (value,) if (value in allowed or value in request_text) else ()
        else:
            candidates = ()
        for domain in candidates:
            domain = domain.strip().lower()
            if domain and domain not in resolved:
                resolved.append(domain)
    return resolved


def normalize_queries(
    queries: Iterable[str], *, limit: int = 3, max_chars: int = 200
) -> list[str]:
    """Printable, single-spaced, length-capped queries; at most ``limit``."""

    cleaned: list[str] = []
    for raw in queries:
        printable = "".join(ch for ch in str(raw or "") if ch.isprintable())
        text = re.sub(r"\s+", " ", printable).strip()[:max_chars].strip()
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= max(0, int(limit)):
            break
    return cleaned


def with_site_filter(query: str, domains: Sequence[str]) -> str:
    """Restricts a query to the resolved domains using engine site: syntax."""

    if not domains:
        return query
    if len(domains) == 1:
        return f"site:{domains[0]} {query}"
    return f"({' OR '.join(f'site:{domain}' for domain in domains)}) {query}"


def domains_in_text(text: str) -> set[str]:
    """Bare hostnames mentioned in a request; used only to validate domains."""

    return {match.group(0).lower() for match in _HOST_PATTERN.finditer(str(text or ""))}
