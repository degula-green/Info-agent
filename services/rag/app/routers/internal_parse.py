from fastapi import APIRouter, HTTPException, Header, Depends
from pathlib import Path
import tempfile
from dataclasses import dataclass

from app.application.processing.preflight import validate_attachment, PreflightError
from app.application.processing.parser_router import ParserRouter
from app.infrastructure.parsing.mineru import MinerUClient, MinerUResultNormalizer
from app.infrastructure.parsing.local import LocalDocumentParser
from app.config import settings
from app.infrastructure.http import HttpClient
from minio import Minio

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@dataclass
class AttachmentPreflightContext:
    """轻量预检上下文"""
    file_name: str
    mime_type: str
    size_bytes: int | None = None


def _verify_internal_token(x_rag_internal_token: str | None = Header(default=None)):
    """内部API鉴权"""
    expected_token = settings.rag_internal_token
    if not expected_token:
        raise HTTPException(500, "RAG_INTERNAL_TOKEN未配置")
    if x_rag_internal_token != expected_token:
        raise HTTPException(403, "内部API鉴权失败")


@router.post("/parse-attachment")
def parse_attachment_internal(
    request: dict,
    _: None = Depends(_verify_internal_token)
):
    """
    内部解析接口（仅Agent服务调用）

    输入:
    {
      "attachment_id": "uuid",
      "owner_id": "user_xxx",
      "file_name": "doc.pdf",
      "mime_type": "application/pdf"
    }

    输出:
    {
      "attachment_id": "uuid",
      "blocks": [{"type": "text", "text": "...", "page_number": 1, ...}],
      "page_count": 10
    }
    """

    attachment_id = request.get("attachment_id")
    owner_id = request.get("owner_id")
    file_name = request.get("file_name")
    mime_type = request.get("mime_type")

    # 从MinIO下载到临时文件。同步函数让 FastAPI 在线程池里执行，
    # 避免阻塞的 MinIO/MinerU 调用卡住事件循环。
    minio_client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure
    )

    ext_map = {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
    }
    ext = ext_map.get(mime_type, "")
    minio_object_name = f"{owner_id}/{attachment_id}{ext}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / f"attachment{ext}"

        try:
            minio_client.fget_object(
                bucket_name=settings.agent_attachment_bucket,
                object_name=minio_object_name,
                file_path=str(tmp_path)
            )
        except Exception as e:
            raise HTTPException(404, f"附件不存在: {str(e)}")

        # 预检校验
        context = AttachmentPreflightContext(
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=tmp_path.stat().st_size
        )

        try:
            preflight = validate_attachment(tmp_path, context)
        except PreflightError as e:
            raise HTTPException(400, f"文件校验失败: {e.code} - {str(e)}")

        # 解析
        parser_router = ParserRouter(
            local=LocalDocumentParser(),
            mineru=MinerUClient(http=HttpClient()),
            normalizer=MinerUResultNormalizer()
        )

        parse_root = Path(tmpdir) / "parse"
        parse_root.mkdir()

        # 构造最小ResourceContext
        from app.domain.rag import ResourceContext
        parse_context = ResourceContext(
            knowledge_item_id=attachment_id,
            resource_type="attachment",
            resource_id=attachment_id,
            knowledge_base_id="temp",
            scope_type="organization",
            scope_id="temp",
            content_version=1,
            content_hash=preflight.sha256,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=preflight.size_bytes,
            object_ref=None
        )

        try:
            parsed_doc = parser_router.parse(preflight, parse_context, parse_root)
        except Exception as e:
            # DOCX降级
            if preflight.extension == "docx":
                try:
                    parsed_doc = parser_router.local.parse_docx(tmp_path)
                except Exception:
                    raise HTTPException(500, f"解析失败: {str(e)}")
            else:
                raise HTTPException(500, f"解析失败: {str(e)}")

        # 转换为返回格式
        result = {
            "attachment_id": attachment_id,
            "blocks": [
                {
                    "type": block.type,
                    "text": block.text,
                    "page_number": block.page_number,
                    "searchable_text": block.searchable_text,
                    "heading_path": list(block.heading_path),
                    "level": block.metadata.get("level"),
                }
                for block in parsed_doc.blocks
            ],
            "page_count": len(set(
                b.page_number for b in parsed_doc.blocks
                if b.page_number is not None
            ))
        }

        return result
