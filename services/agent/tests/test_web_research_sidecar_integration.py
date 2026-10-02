"""Sidecar integration: the real HTTP clients over a real socket.

The capability tests stub ``_open``; these start a loopback server that answers
the way SearXNG and Crawl4AI answer, so URL building, query encoding, JSON
parsing, status handling and the fetch guard are exercised through an actual
socket instead of a mock. The third-party containers still need Docker; what is
proven here is that the Agent's side of both contracts works.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from app.capabilities.web_research import WebResearchCapability
from app.infrastructure.search.searxng import SearxngSearchProvider
from app.infrastructure.web.content_reader import ContentReader
from app.infrastructure.web.crawl4ai_client import Crawl4AIClient
from app.providers.page_fetcher import HttpPageFetcher, PageFetchError

PAGE_HTML = (
    "<html><head><title>北京市网络协议</title></head><body><p>"
    + "协议正文内容。" * 80
    + "</p></body></html>"
)
SHORT_HTML = "<html><head><title>加载中</title></head><body><p>正在加载</p></body></html>"
RENDERED_MARKDOWN = "# 渲染后的正文\n" + "渲染内容。" * 60
COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker" / "docker-compose.yml"
RENDER_AUTHORIZATIONS: list[str | None] = []


class _Handler(BaseHTTPRequestHandler):
    """Answers the two sidecar contracts plus a few pages to read."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path.startswith("/search"):
            if "q=fail" in self.path:
                self._send(503, b"upstream down", "text/plain")
                return
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            self._send(
                200,
                json.dumps(
                    {
                        "results": [
                            # Real engines echo the query back in the result text;
                            # the capability drops hits that mention nothing the
                            # query asked about.
                            {
                                "url": f"{base}/page",
                                "title": "北京市网络协议 - 静态页",
                                "content": "北京市网络协议正文摘要",
                            },
                            {
                                "url": f"{base}/dynamic",
                                "title": "北京市网络协议 - 动态页",
                                "content": "北京市网络协议 动态渲染摘要",
                            },
                        ]
                    }
                ).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/page":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/dynamic":
            self._send(200, SHORT_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/missing":
            self._send(404, b"gone", "text/plain")
            return
        self._send(404, b"gone", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        # /md is the official image's single-page endpoint.
        if self.path != "/md":
            self._send(404, b"gone", "text/plain")
            return
        RENDER_AUTHORIZATIONS.append(self.headers.get("Authorization"))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        url = str(body.get("url") or "")
        self._send(
            200,
            json.dumps(
                {
                    "url": url,
                    "final_url": url,
                    "status": 200,
                    "title": "渲染后的标题",
                    "markdown": RENDERED_MARKDOWN,
                    "links": [],
                }
            ).encode("utf-8"),
            "application/json",
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # keep the test output clean
        return


@pytest.fixture()
def sidecar() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def build(sidecar_url: str) -> WebResearchCapability:
    # The loopback switch is what lets a local test read a local page; the
    # guard's default behaviour is asserted separately below.
    fetcher = HttpPageFetcher(allow_private_addresses=True, timeout_seconds=5)
    return WebResearchCapability(
        ContentReader(
            fetcher,
            renderer=Crawl4AIClient(
                sidecar_url, api_token="test-token", timeout_seconds=5
            ),
            min_text_chars=500,
        ),
        search_provider=SearxngSearchProvider(sidecar_url, timeout_seconds=5),
        aliases={},
    )


def test_search_then_read_runs_end_to_end_over_sockets(sidecar: str) -> None:
    capability = build(sidecar)

    result = capability.execute(
        capability.validate(
            {"request": "搜索北京市网络协议并总结", "queries": ["北京市网络协议"]}
        )
    )

    urls = [item["url"] for item in result["evidence"]]
    assert urls == [f"{sidecar}/page", f"{sidecar}/dynamic"]
    methods = {item["url"]: item["fetch_method"] for item in result["evidence"]}
    # The long static page is read directly; the short one goes to the renderer.
    assert methods[f"{sidecar}/page"] == "static"
    assert methods[f"{sidecar}/dynamic"] == "crawl4ai"

    static_evidence = result["evidence"][0]
    assert static_evidence["title"] == "北京市网络协议"
    assert "协议正文内容" in static_evidence["text"]
    # The search snippet is a ranking hint; it must not reach the evidence.
    assert "搜索摘要" not in static_evidence["text"]
    assert static_evidence["quote"] in static_evidence["text"]


def test_a_search_outage_is_reported_and_the_links_still_work(sidecar: str) -> None:
    capability = build(sidecar)

    result = capability.execute(
        capability.validate(
            {
                "request": f"读一下 {sidecar}/page",
                "urls": [f"{sidecar}/page"],
                "queries": ["fail"],
            }
        )
    )

    assert [item["url"] for item in result["evidence"]] == [f"{sidecar}/page"]
    assert any("搜索暂不可用" in warning for warning in result["warnings"])


def test_the_renderer_covers_a_page_a_plain_get_could_not_body(sidecar: str) -> None:
    """A rendered page is evidence with its own provenance."""

    capability = build(sidecar)

    result = capability.execute(
        capability.validate({"request": "搜索动态页", "queries": ["动态页"]})
    )

    dynamic = next(item for item in result["evidence"] if item["url"].endswith("/dynamic"))
    assert dynamic["fetch_method"] == "crawl4ai"
    assert "渲染内容" in dynamic["text"]
    # The official image puts every endpoint behind a bearer token.
    assert RENDER_AUTHORIZATIONS[-1] == "Bearer test-token"


def test_the_fetch_guard_refuses_loopback_unless_switched_on() -> None:
    """The default deployment cannot be steered at internal addresses."""

    with pytest.raises(PageFetchError) as excinfo:
        HttpPageFetcher(timeout_seconds=2).fetch("http://127.0.0.1:9/page")

    assert excinfo.value.code in {"blocked_address", "page_fetch_retryable"}


def test_the_compose_file_declares_both_sidecars_with_json_search() -> None:
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))

    services = document["services"]
    assert "searxng" in services
    assert "crawl4ai" in services
    # Loopback only: the search engine must not become a public proxy.
    assert services["searxng"]["ports"] == ["127.0.0.1:18080:8080"]
    assert services["crawl4ai"]["ports"] == ["127.0.0.1:11235:11235"]
    # Chromium needs more shared memory than Docker's default.
    assert services["crawl4ai"]["shm_size"] == "1g"
    assert services["crawl4ai"]["environment"]["CRAWL4AI_API_TOKEN"]

    settings_path = COMPOSE_PATH.parent / "searxng" / "settings.yml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert "json" in settings["search"]["formats"]
