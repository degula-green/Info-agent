from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain.models import AttachmentContext


class StorageError(RuntimeError):
    pass


class ArtifactStore:
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        raise NotImplementedError

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> str:
        return self.put_bytes(key, value.encode("utf-8"), content_type)

    def put_json(self, key: str, value: dict[str, Any]) -> str:
        return self.put_text(key, json.dumps(value, ensure_ascii=False, indent=2), "application/json")

    def download_source(self, context: AttachmentContext, destination: Path) -> int:
        raise NotImplementedError


class LocalArtifactStore(ArtifactStore):
    """Local fallback used for fixtures and development without MinIO."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.preprocess_work_dir / "artifacts").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        clean = key.replace("\\", "/").lstrip("/")
        path = (self.root / clean).resolve()
        if self.root not in path.parents and path != self.root:
            raise StorageError("artifact key escapes local storage root")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}"

    def download_source(self, context: AttachmentContext, destination: Path) -> int:
        source = context.file_path or context.object_ref
        if not source:
            raise StorageError("attachment has no file_path or object_ref")
        if source.startswith("file://"):
            source = source[7:]
        path = Path(source)
        if path.exists() and path.is_file():
            return _copy_limited(path, destination, settings.preprocess_max_file_bytes)
        if source.startswith(("http://", "https://")):
            return _download_url(source, destination, settings.preprocess_max_file_bytes)
        # A relative object reference can point into the local source bucket.
        candidate = self._path(source)
        if candidate.exists() and candidate.is_file():
            return _copy_limited(candidate, destination, settings.preprocess_max_file_bytes)
        raise StorageError("local source object was not found")


class MinioArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        if not settings.minio_endpoint:
            raise StorageError("RAG_MINIO_ENDPOINT is required")
        try:
            from minio import Minio
        except ModuleNotFoundError as exc:
            raise StorageError("minio package is required for MinIO storage") from exc
        endpoint = settings.minio_endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.client = Minio(
            endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def _ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        from io import BytesIO

        bucket = settings.minio_derived_bucket
        self._ensure_bucket(bucket)
        self.client.put_object(bucket, key, BytesIO(data), len(data), content_type=content_type)
        return f"minio://{bucket}/{key}"

    def download_source(self, context: AttachmentContext, destination: Path) -> int:
        if context.file_path:
            local_path = Path(context.file_path)
            if local_path.exists() and local_path.is_file():
                return _copy_limited(local_path, destination, settings.preprocess_max_file_bytes)
        source = context.object_ref
        if not source:
            raise StorageError("attachment has no object_ref")
        if source.startswith(("http://", "https://")):
            return _download_url(source, destination, settings.preprocess_max_file_bytes)
        key = source.removeprefix("minio://")
        bucket = settings.minio_source_bucket
        if "/" in key and key.split("/", 1)[0] == bucket:
            key = key.split("/", 1)[1]
        try:
            response = self.client.get_object(bucket, key)
            try:
                return _write_response_limited(response, destination, settings.preprocess_max_file_bytes)
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            raise StorageError("MinIO source object could not be downloaded") from exc


def build_artifact_store() -> ArtifactStore:
    return MinioArtifactStore() if settings.minio_endpoint else LocalArtifactStore()


def derived_key(context: AttachmentContext, run_id: str, suffix: str) -> str:
    digest = context.source_content_hash or "unknown"
    safe_hash = re.sub(r"[^0-9A-Fa-f]", "", digest.removeprefix("sha256:"))[:40] or "unknown"
    safe_attachment = hashlib.sha256(context.attachment_id.encode("utf-8")).hexdigest()[:24]
    safe_run = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:20]
    return (
        f"{settings.minio_derived_prefix}/{safe_attachment}/"
        f"{context.content_version}/{safe_run}/{safe_hash}/{suffix.lstrip('/')}"
    )


def _copy_limited(source: Path, destination: Path, limit: int) -> int:
    if source.stat().st_size > limit:
        raise StorageError("source file exceeds configured size limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination.stat().st_size


def _download_url(url: str, destination: Path, limit: int) -> int:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=settings.knowledge_timeout_seconds) as response:
            return _write_response_limited(response, destination, limit)
    except Exception as exc:
        raise StorageError("source URL could not be downloaded") from exc


def _write_response_limited(response: Any, destination: Path, limit: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with destination.open("wb") as stream:
        while True:
            chunk = response.read(min(1024 * 1024, limit + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise StorageError("source object exceeds configured size limit")
            stream.write(chunk)
    return total
