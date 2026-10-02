from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from datetime import datetime, timedelta
import uuid
from io import BytesIO

from app.config import settings
from app.auth import AuthenticatedUser, current_user
from app.infrastructure.attachment_store import RedisAttachmentStore, AttachmentMetadata
from minio import Minio

router = APIRouter(prefix="/api/agent/v1")

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}


def get_minio_client() -> Minio:
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False
    )


def get_attachment_store() -> RedisAttachmentStore:
    import redis
    redis_url = (settings.redis_url or "").strip()
    if not redis_url:
        raise HTTPException(status_code=503, detail="attachment storage is not configured")
    r = redis.from_url(redis_url)
    return RedisAttachmentStore(r, settings.attachment_ttl_hours)


@router.post("/attachments")
async def upload_attachment(
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(current_user),
    minio_client: Minio = Depends(get_minio_client),
    store: RedisAttachmentStore = Depends(get_attachment_store)
):
    """
    上传临时附件

    安全措施：
    1. 服务端生成UUIDv4（保证唯一）
    2. 存MinIO独立bucket
    3. Redis记录owner_id（权限隔离）
    4. 流式读取避免内存打爆
    """

    owner_id = user.user_id

    # 流式读取并校验大小
    chunks = []
    size_bytes = 0
    while chunk := await file.read(1024 * 1024):  # 1MB chunks
        size_bytes += len(chunk)
        if size_bytes > settings.attachment_max_size_bytes:
            raise HTTPException(
                400,
                f"文件过大（>{settings.attachment_max_size_bytes/1024/1024:.0f}MB）"
            )
        chunks.append(chunk)

    content = b"".join(chunks)

    # 校验MIME类型
    mime_type = file.content_type or "application/octet-stream"
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(400, f"不支持的文件类型: {mime_type}")

    # 生成唯一ID（UUIDv4）
    attachment_id = str(uuid.uuid4())

    # 白名单化文件名
    safe_filename = "".join(
        c for c in file.filename
        if c.isalnum() or c in ".-_"
    )[:200]
    if not safe_filename:
        safe_filename = "unnamed"

    # MinIO对象名
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"
    }
    ext = ext_map.get(mime_type, "")
    minio_object_name = f"{owner_id}/{attachment_id}{ext}"

    # 上传到MinIO
    minio_client.put_object(
        bucket_name=settings.attachment_minio_bucket,
        object_name=minio_object_name,
        data=BytesIO(content),
        length=size_bytes,
        content_type=mime_type
    )

    # 保存元信息到Redis
    now = datetime.utcnow()
    metadata = AttachmentMetadata(
        attachment_id=attachment_id,
        owner_id=owner_id,
        file_name=safe_filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        minio_object_name=minio_object_name,
        uploaded_at=now.isoformat(),
        expires_at=(now + timedelta(hours=settings.attachment_ttl_hours)).isoformat()
    )
    store.save(metadata)

    return {
        "attachment_id": attachment_id,
        "file_name": safe_filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes
    }
