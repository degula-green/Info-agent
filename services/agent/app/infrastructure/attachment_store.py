"""附件元信息存储（Redis）"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import redis


@dataclass
class AttachmentMetadata:
    attachment_id: str
    owner_id: str
    file_name: str
    mime_type: str
    size_bytes: int
    minio_object_name: str
    uploaded_at: str
    expires_at: str


class RedisAttachmentStore:
    def __init__(self, redis_client: redis.Redis, ttl_hours: int = 24):
        self.redis = redis_client
        self.ttl_seconds = ttl_hours * 3600

    def save(self, metadata: AttachmentMetadata) -> None:
        """保存附件元信息"""
        key = f"attachment:{metadata.attachment_id}"
        value = json.dumps({
            "attachment_id": metadata.attachment_id,
            "owner_id": metadata.owner_id,
            "file_name": metadata.file_name,
            "mime_type": metadata.mime_type,
            "size_bytes": metadata.size_bytes,
            "minio_object_name": metadata.minio_object_name,
            "uploaded_at": metadata.uploaded_at,
            "expires_at": metadata.expires_at
        })
        self.redis.setex(key, self.ttl_seconds, value)

    def get(self, attachment_id: str) -> dict | None:
        """获取附件元信息"""
        key = f"attachment:{attachment_id}"
        value = self.redis.get(key)
        if not value:
            return None
        return json.loads(value)

    def validate_ownership(self, attachment_id: str, owner_id: str) -> bool:
        """校验附件所有权"""
        metadata = self.get(attachment_id)
        if not metadata:
            return False
        return metadata["owner_id"] == owner_id
