"""The Agent's own temporary attachment bucket.

Uploads (the weekly-report template the user attaches) and generated artifacts
(the rendered .docx) both live here: an object in the ``agent-temp`` MinIO
bucket plus a Redis metadata record that expires after 24 hours. The metadata
record is also the ownership check for the download endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from uuid import uuid4

from app.infrastructure.attachment_store import AttachmentMetadata, RedisAttachmentStore

EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class AgentAttachmentError(RuntimeError):
    code = "attachment_unavailable"
    classification = "retryable_error"


class AgentAttachmentNotFound(AgentAttachmentError):
    code = "attachment_not_found"
    classification = "permanent_error"


def safe_file_name(value: str) -> str:
    """Keep the characters the storage key and the Content-Disposition allow."""

    cleaned = "".join(
        character
        for character in str(value or "")
        if character.isalnum() or character in ".-_ （）()"
    ).strip()
    return cleaned[:200] or "unnamed"


def extension_for(mime_type: str, file_name: str = "") -> str:
    if mime_type in EXTENSIONS:
        return EXTENSIONS[mime_type]
    lowered = str(file_name or "").lower()
    for suffix in (".docx", ".pdf", ".png", ".jpg", ".jpeg"):
        if lowered.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ""


class AgentAttachmentStore:
    """Reads and writes the Agent's temporary attachments."""

    def __init__(
        self,
        *,
        minio,
        metadata_store: RedisAttachmentStore,
        bucket: str,
        ttl_hours: int = 24,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.minio = minio
        self.metadata_store = metadata_store
        self.bucket = bucket
        self.ttl_hours = max(int(ttl_hours), 1)
        self.max_bytes = max(int(max_bytes), 1)

    def ensure_bucket(self) -> None:
        """The temp bucket is created on first write; a missing one is not fatal."""

        try:
            if not self.minio.bucket_exists(self.bucket):
                self.minio.make_bucket(self.bucket)
        except Exception:  # noqa: BLE001 - a pre-provisioned bucket needs no call
            pass

    def metadata(self, attachment_id: str) -> dict[str, Any] | None:
        return self.metadata_store.get(str(attachment_id or "").strip())

    def read(self, attachment_id: str) -> bytes:
        record = self.metadata(attachment_id)
        if not record:
            raise AgentAttachmentNotFound("attachment metadata was not found")
        response = None
        try:
            response = self.minio.get_object(
                self.bucket, str(record.get("minio_object_name") or "")
            )
            return response.read()
        except Exception as exc:  # noqa: BLE001 - MinIO raises S3Error subtypes
            raise AgentAttachmentError(f"attachment download failed: {exc}") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                    response.release_conn()
                except Exception:  # noqa: BLE001 - the body is already read
                    pass

    def save(
        self,
        *,
        owner_id: str,
        file_name: str,
        mime_type: str,
        data: bytes,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        if len(data) > self.max_bytes:
            raise AgentAttachmentError("attachment exceeds the size limit")
        identifier = str(attachment_id or uuid4())
        safe_name = safe_file_name(file_name)
        object_name = (
            f"{owner_id}/{identifier}{extension_for(mime_type, safe_name)}"
        )
        self.ensure_bucket()
        self.minio.put_object(
            bucket_name=self.bucket,
            object_name=object_name,
            data=BytesIO(data),
            length=len(data),
            content_type=mime_type,
        )
        now = datetime.now(timezone.utc)
        metadata = AttachmentMetadata(
            attachment_id=identifier,
            owner_id=str(owner_id),
            file_name=safe_name,
            mime_type=mime_type,
            size_bytes=len(data),
            minio_object_name=object_name,
            uploaded_at=now.isoformat(),
            expires_at=(now + timedelta(hours=self.ttl_hours)).isoformat(),
        )
        self.metadata_store.save(metadata)
        return {
            "attachment_id": identifier,
            "owner_id": str(owner_id),
            "file_name": safe_name,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "minio_object_name": object_name,
            "uploaded_at": metadata.uploaded_at,
            "expires_at": metadata.expires_at,
        }
