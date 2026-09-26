from __future__ import annotations

import hashlib
import mimetypes
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain.models import AttachmentContext


class PreflightError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreflightResult:
    path: Path
    extension: str
    mime_type: str
    size_bytes: int
    sha256: str
    category: str
    page_count: int | None = None
    warnings: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


LOCAL_EXTENSIONS = {"txt", "md", "json", "csv", "tsv"}
MINERU_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg"}
UNSUPPORTED_EXTENSIONS = {
    "html", "htm", "doc", "ppt", "xls", "zip", "7z", "rar", "exe",
    "mp3", "mp4", "wav", "avi", "mov", "encrypted",
}

_MIME_BY_EXTENSION = {
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/plain"},
    "json": {"application/json", "text/json", "text/plain"},
    "csv": {"text/csv", "application/csv", "text/plain"},
    "tsv": {"text/tab-separated-values", "text/plain"},
    "pdf": {"application/pdf"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "png": {"image/png"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
}


def validate_attachment(path: Path, context: Any) -> PreflightResult:
    """Validate a downloaded source before any parser or cloud upload sees it."""
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise PreflightError("SOURCE_NOT_FOUND", "attachment source was not found")
    size = path.stat().st_size
    if size <= 0:
        raise PreflightError("EMPTY_FILE", "attachment source is empty")
    extension = Path(context.file_name).suffix.lower().lstrip(".")
    if not extension:
        extension = path.suffix.lower().lstrip(".")
    type_limit = settings.preprocess_max_file_bytes
    if extension in {"txt", "md"}:
        type_limit = min(type_limit, settings.preprocess_max_text_bytes)
    elif extension == "json":
        type_limit = min(type_limit, settings.preprocess_max_text_bytes)
    elif extension in {"csv", "tsv"}:
        type_limit = min(type_limit, settings.preprocess_max_table_bytes)
    elif extension in {"docx", "pptx", "xlsx"}:
        type_limit = min(type_limit, settings.preprocess_max_office_bytes)
    elif extension == "pdf":
        type_limit = min(type_limit, settings.preprocess_max_pdf_bytes)
    elif extension in {"png", "jpg", "jpeg"}:
        type_limit = min(type_limit, settings.preprocess_max_image_bytes)
    if size > type_limit:
        raise PreflightError("FILE_TOO_LARGE", "attachment exceeds configured size limit")

    if extension in UNSUPPORTED_EXTENSIONS or extension not in LOCAL_EXTENSIONS | MINERU_EXTENSIONS:
        raise PreflightError("UNSUPPORTED_FORMAT", f"unsupported attachment format: {extension or 'unknown'}")

    actual_mime = (context.mime_type or mimetypes.guess_type(context.file_name)[0] or "application/octet-stream").lower().split(";", 1)[0].strip()
    expected_mimes = _MIME_BY_EXTENSION.get(extension, set())
    warnings: list[str] = []
    if expected_mimes and actual_mime not in expected_mimes and actual_mime != "application/octet-stream":
        warnings.append("mime_extension_mismatch")

    if context.size_bytes and int(context.size_bytes) != size:
        raise PreflightError("SIZE_MISMATCH", "attachment size does not match source metadata")

    with path.open("rb") as source:
        prefix = source.read(8192)
    _validate_magic(extension, prefix)
    if extension == "pdf" and _contains_pdf_encryption(path):
        raise PreflightError("PASSWORD_PROTECTED", "encrypted PDF documents are not supported")
    if extension in {"docx", "pptx", "xlsx"}:
        _validate_office_package(extension, path)

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    sha256 = digest.hexdigest()
    if context.source_content_hash:
        expected = context.source_content_hash.removeprefix("sha256:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise PreflightError("HASH_INVALID", "source_content_hash must be a SHA-256 hex digest")
        if expected != sha256:
            raise PreflightError("HASH_MISMATCH", "attachment hash does not match source metadata")

    page_count = _pdf_page_count(prefix, path) if extension == "pdf" else None
    if page_count is not None and page_count > settings.preprocess_max_pages:
        raise PreflightError("PAGE_LIMIT_EXCEEDED", "attachment exceeds configured page limit")
    category = "local" if extension in LOCAL_EXTENSIONS else "mineru"
    return PreflightResult(
        path=path,
        extension=extension,
        mime_type=actual_mime,
        size_bytes=size,
        sha256=sha256,
        category=category,
        page_count=page_count,
        warnings=tuple(warnings),
    )


def _validate_magic(extension: str, prefix: bytes) -> None:
    if extension == "pdf" and not prefix.startswith(b"%PDF-"):
        raise PreflightError("MAGIC_MISMATCH", "file is not a PDF")
    if extension == "png" and not prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PreflightError("MAGIC_MISMATCH", "file is not a PNG")
    if extension in {"jpg", "jpeg"} and not prefix.startswith(b"\xff\xd8\xff"):
        raise PreflightError("MAGIC_MISMATCH", "file is not a JPEG")
    if extension in {"docx", "pptx", "xlsx"} and not prefix.startswith(b"PK\x03\x04"):
        raise PreflightError("MAGIC_MISMATCH", "Office Open XML file is not a ZIP package")


def _validate_office_package(extension: str, path: Path) -> None:
    """Reject arbitrary ZIP files renamed with an Office extension."""
    required_prefix = {
        "docx": "word/",
        "pptx": "ppt/",
        "xlsx": "xl/",
    }[extension]
    try:
        with zipfile.ZipFile(path) as package:
            names = {name.replace("\\", "/") for name in package.namelist()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise PreflightError("MAGIC_MISMATCH", "Office Open XML package is not a valid ZIP") from exc
    if "[Content_Types].xml" not in names or not any(name.startswith(required_prefix) for name in names):
        raise PreflightError("MAGIC_MISMATCH", f"file is not a valid {extension.upper()} package")


def _contains_pdf_encryption(path: Path) -> bool:
    """Scan the whole bounded source; /Encrypt may occur after the header."""
    try:
        with path.open("rb") as source:
            carry = b""
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    return False
                data = carry + block
                if b"/Encrypt" in data:
                    return True
                carry = data[-16:]
    except OSError as exc:
        raise PreflightError("SOURCE_READ_FAILED", "could not read PDF for encryption check") from exc


def _pdf_page_count(prefix: bytes, path: Path) -> int | None:
    # This is a conservative preflight estimate; MinerU remains authoritative.
    try:
        data = prefix + path.read_bytes()[len(prefix):]
        count = len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", data))
        return count or None
    except OSError:
        return None
