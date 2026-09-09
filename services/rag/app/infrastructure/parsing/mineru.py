from __future__ import annotations

import json
import mimetypes
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import settings
from app.domain.models import CanonicalBlock, ParsedDocument
from app.infrastructure.http import HttpClient, IntegrationError, join_url
from app.infrastructure.parsing.local import markdown_blocks, normalize_newlines


SUPPORTED_MINERU = {"pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg"}
DONE_STATES = {"done", "success", "succeeded", "completed", "finished"}
FAILED_STATES = {"failed", "error", "cancelled", "canceled"}


class MinerUError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class UnsafeArchiveError(MinerUError):
    pass


class MinerUClient:
    """MinerU v4 cloud adapter. The rest of the service knows no HTTP details."""

    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()
        self.base_url = settings.mineru_api_base_url.rstrip("/")

    def submit_by_url(self, source_url: str, file_name: str, *, data_id: str | None = None) -> str:
        self._require_configured()
        body = self._task_body(source_url, file_name, data_id=data_id)
        value = self._request_json("POST", join_url(self.base_url, "/extract/task"), body)
        task_id = _find_id(value, "task_id")
        if not task_id:
            raise MinerUError("MinerU response did not contain task_id")
        return task_id

    def submit_by_upload(self, path: Path, *, data_id: str | None = None) -> str:
        self._require_configured()
        file_name = path.name
        # v4 returns one-time upload URLs. The response shape has changed
        # between API revisions, so accept both data.file_urls and a flat list.
        response = self._request_json(
            "POST",
            join_url(self.base_url, "/file-urls/batch"),
            self._upload_request_body(file_name, data_id=data_id),
        )
        upload_url = _first_url(response)
        if not upload_url:
            raise MinerUError("MinerU did not return an upload URL")
        self._upload(upload_url, path)
        task_id = _find_id(response, "task_id")
        if task_id:
            return task_id
        # The v4 signed-upload endpoint starts extraction after PUT and returns
        # a batch_id. Polling it uses /extract-results/batch/{batch_id}, not
        # the single-file /extract/task endpoint.
        batch_id = _find_id(response, "batch_id")
        if not batch_id:
            raise MinerUError("MinerU response did not contain task_id or batch_id")
        return f"batch:{batch_id}"

    def poll(self, task_id: str) -> dict[str, Any]:
        self._require_configured()
        deadline = time.monotonic() + settings.mineru_task_max_wait_seconds
        interval = max(0.5, settings.mineru_poll_interval_seconds)
        last: dict[str, Any] = {}
        is_batch = str(task_id).startswith("batch:")
        raw_id = str(task_id).removeprefix("batch:")
        endpoint = f"/extract-results/batch/{raw_id}" if is_batch else f"/extract/task/{raw_id}"
        while time.monotonic() < deadline:
            last = self._request_json("GET", join_url(self.base_url, endpoint), None)
            state = _state(last)
            if state in DONE_STATES:
                return last
            if state in FAILED_STATES:
                raise MinerUError("MinerU task failed")
            time.sleep(interval)
            interval = min(interval * 1.5, 30.0)
        raise MinerUError("MinerU task timed out", retryable=True)

    def download_result(self, task_response: dict[str, Any], destination: Path) -> Path:
        url = _find_url(task_response, ("full_zip_url", "zip_url", "result_url", "download_url"))
        if not url:
            raise MinerUError("MinerU task completed without a result ZIP")
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"Accept": "application/zip"})
        try:
            with urllib.request.urlopen(request, timeout=settings.mineru_result_download_timeout_seconds) as response:
                with destination.open("wb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
        except Exception as exc:
            raise MinerUError("MinerU result download failed", retryable=True) from exc
        return destination

    def _require_configured(self) -> None:
        if not self.base_url or not settings.mineru_api_token:
            raise MinerUError("MINERU_API_BASE_URL and MINERU_API_TOKEN are required")

    def _task_body(self, source_url: str, file_name: str, *, data_id: str | None) -> dict[str, Any]:
        return {
            "url": source_url,
            "model_version": settings.mineru_model_version,
            "is_ocr": settings.mineru_enable_ocr,
            "enable_formula": settings.mineru_enable_formula,
            "enable_table": settings.mineru_enable_table,
            "language": settings.mineru_language,
            "data_id": data_id or file_name,
            "file_name": file_name,
            "no_cache": settings.mineru_no_cache,
            "cache_tolerance": settings.mineru_cache_tolerance_seconds,
        }

    def _upload_request_body(self, file_name: str, *, data_id: str | None) -> dict[str, Any]:
        return {
            "files": [{"name": file_name, "data_id": data_id or uuid.uuid4().hex}],
            "model_version": settings.mineru_model_version,
            "is_ocr": settings.mineru_enable_ocr,
            "enable_formula": settings.mineru_enable_formula,
            "enable_table": settings.mineru_enable_table,
            "language": settings.mineru_language,
            "no_cache": settings.mineru_no_cache,
            "cache_tolerance": settings.mineru_cache_tolerance_seconds,
        }

    def _request_json(self, method: str, url: str, body: Any | None) -> Any:
        attempts = max(1, settings.mineru_max_retries + 1)
        for attempt in range(attempts):
            try:
                return self.http.request(
                    method,
                    url,
                    body=body,
                    token=settings.mineru_api_token,
                    timeout=settings.mineru_http_timeout_seconds,
                ).json()
            except IntegrationError as exc:
                if not exc.retryable or attempt + 1 >= attempts:
                    raise MinerUError("MinerU API request failed", retryable=exc.retryable) from exc
                time.sleep(min(2**attempt, 8))
        raise MinerUError("MinerU API request failed", retryable=True)

    def _upload(self, url: str, path: Path) -> None:
        request = urllib.request.Request(
            url,
            data=path.read_bytes(),
            headers={"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.mineru_http_timeout_seconds):
                return
        except Exception as exc:
            raise MinerUError("upload to MinerU failed", retryable=True) from exc


def safe_extract_zip(archive: Path, destination: Path) -> list[Path]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > settings.preprocess_max_unpack_files:
                raise UnsafeArchiveError("MinerU archive contains too many files")
            for member in members:
                name = member.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
                    raise UnsafeArchiveError("MinerU archive contains an unsafe path")
                if member.is_dir():
                    continue
                if member.external_attr >> 16 & 0o170000 == 0o120000:
                    raise UnsafeArchiveError("MinerU archive contains a symbolic link")
                total += member.file_size
                if total > settings.preprocess_max_unpack_bytes:
                    raise UnsafeArchiveError("MinerU archive exceeds unpack size limit")
                target = (destination / Path(*path.parts)).resolve()
                if destination not in target.parents:
                    raise UnsafeArchiveError("MinerU archive escapes destination")
                normalized_name = target.relative_to(destination).as_posix()
                if normalized_name in seen:
                    raise UnsafeArchiveError("MinerU archive contains duplicate paths")
                seen.add(normalized_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("MinerU result is not a valid ZIP") from exc
    return extracted


class MinerUResultNormalizer:
    def normalize(self, root: Path, *, parser_version: str = "mineru-v4") -> ParsedDocument:
        markdown_path = _find_file(root, "full.md") or _find_suffix(root, ".md")
        markdown = normalize_newlines(markdown_path.read_text(encoding="utf-8", errors="replace")) if markdown_path else ""
        content_path = _find_content_list(root)
        blocks = self._blocks_from_content_list(content_path, root) if content_path else []
        if not blocks:
            blocks = markdown_blocks(markdown)
        if not markdown and blocks:
            markdown = "\n\n".join(block.searchable_text for block in blocks if block.searchable_text)
        assets = [str(path.relative_to(root).as_posix()) for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}]
        return ParsedDocument(
            markdown=markdown,
            blocks=blocks,
            parser="mineru-vlm",
            parser_version=parser_version,
            asset_refs=assets,
            auxiliary_files=_find_auxiliary_files(root),
        )

    def _blocks_from_content_list(self, path: Path, root: Path) -> list[CanonicalBlock]:
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return []
        entries = value if isinstance(value, list) else value.get("content_list", value.get("blocks", [])) if isinstance(value, dict) else []
        if not isinstance(entries, list):
            return []
        blocks: list[CanonicalBlock] = []
        heading_path: list[str] = []
        for order, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or entry.get("category") or "text").lower()
            text = _entry_text(entry)
            page = _number(entry.get("page_idx", entry.get("page_number")))
            if page is not None and "page_idx" in entry:
                page += 1
            level = _number(entry.get("level"))
            if kind in {"title", "heading", "header"}:
                if level and level > 0:
                    heading_path[:] = heading_path[: level - 1]
                heading_path.append(text)
                blocks.append(CanonicalBlock(page, order, "title", text, heading_path=tuple(heading_path), metadata={"level": level or 1}))
                continue
            if not text and kind not in {"image", "figure"}:
                continue
            block_type = {
                "table": "table",
                "equation": "equation",
                "formula": "equation",
                "image": "image",
                "figure": "image",
                "code": "code",
                "list": "list",
            }.get(kind, "text")
            asset = entry.get("img_path") or entry.get("image_path") or entry.get("asset_ref")
            asset_ref = _safe_relative_asset(root, str(asset)) if asset else None
            html = str(entry.get("table_body") or entry.get("html") or "") or None
            latex = str(entry.get("latex") or entry.get("equation") or "") or None
            bbox = entry.get("bbox")
            metadata = {"raw_type": kind}
            for key in ("slide_number", "sheet_name", "row_start", "row_end", "column_start", "column_end", "language"):
                if entry.get(key) is not None:
                    metadata[key] = entry[key]
            blocks.append(CanonicalBlock(page, order, block_type, text, html=html, latex=latex, asset_ref=asset_ref, bbox=tuple(float(x) for x in bbox) if isinstance(bbox, list) and len(bbox) == 4 else None, heading_path=tuple(heading_path), metadata=metadata))
        return blocks


def _find_file(root: Path, name: str) -> Path | None:
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == name.lower():
            return path
    return None


def _find_suffix(root: Path, suffix: str) -> Path | None:
    values = sorted(path for path in root.rglob(f"*{suffix}") if path.is_file())
    return values[0] if values else None


def _find_content_list(root: Path) -> Path | None:
    values = sorted(path for path in root.rglob("*.json") if "content_list" in path.name.lower())
    return values[0] if values else None


def _entry_text(entry: dict[str, Any]) -> str:
    for key in ("text", "content", "value", "table_body", "latex", "equation", "image_description", "img_caption", "table_caption", "title"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_newlines(value)
    return ""


def _safe_relative_asset(root: Path, value: str) -> str | None:
    value = value.replace("\\", "/").lstrip("/")
    if not value or ".." in PurePosixPath(value).parts:
        return None
    candidate = (root / Path(*PurePosixPath(value).parts)).resolve()
    return value if candidate.exists() and root.resolve() in candidate.parents else None


def _number(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _find_id(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        if value.get(key):
            return str(value[key])
        data = value.get("data")
        if data is not value:
            found = _find_id(data, key)
            if found:
                return found
    return None


def _first_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("file_urls", "urls", "upload_urls"):
            found = _first_url(value.get(key))
            if found:
                return found
        return _first_url(value.get("data"))
    if isinstance(value, list):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _find_url(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
        for nested in value.values():
            found = _find_url(nested, keys)
            if found:
                return found
    return None


def _state(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    data = value.get("data") if isinstance(value.get("data"), dict) else value
    direct = str(data.get("state") or data.get("status") or data.get("task_state") or "").lower()
    if direct:
        return direct
    extract = data.get("extract_result")
    if isinstance(extract, dict):
        return str(extract.get("state") or extract.get("status") or "").lower()
    return ""


def _find_auxiliary_files(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name == "middle.json" or name.endswith("_middle.json"):
            values.setdefault("middle.json", path.relative_to(root).as_posix())
        elif name == "model.json" or name.endswith("_model.json"):
            values.setdefault("model.json", path.relative_to(root).as_posix())
    return values
