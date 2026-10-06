"""Fill picture blocks that carry no text with a vision-generated description.

MinerU reads written text out of a picture, so a screenshot comes back as
ordinary text blocks. When it returns an image block with no text there is
nothing for retrieval to match; this module describes that picture so the
attachment still carries something searchable and quotable.

The enricher never fails an attachment: a vision error leaves the block as it
was. The one exception is an uploaded picture that exceeds the size the vision
model accepts - the owner is told it is unsupported rather than being given a
silently empty parse.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.domain.models import ParsedDocument
from app.infrastructure.vision.client import VisionClient, VisionError

logger = logging.getLogger("rag.vision")

IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

CACHE_KEY_PREFIX = "rag:vision:desc:v1:"


class ImageTooLargeError(RuntimeError):
    """An uploaded picture beyond what the vision model accepts."""


class ImageDescriptionEnricher:
    def __init__(
        self,
        vision: VisionClient,
        *,
        cache: Any | None = None,
        max_images: int = 5,
        max_image_bytes: int = 10 * 1024 * 1024,
        cache_ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self.vision = vision
        self.cache = cache
        self.max_images = max(int(max_images), 0)
        self.max_image_bytes = max(int(max_image_bytes), 1)
        self.cache_ttl_seconds = max(int(cache_ttl_seconds), 0)

    def enrich(
        self,
        document: ParsedDocument,
        *,
        root: Path,
        source_path: Path | None = None,
        source_mime_type: str = "",
    ) -> ParsedDocument:
        targets = [
            index
            for index, block in enumerate(document.blocks)
            if str(block.type or "").strip().lower() == "image"
            and not str(block.text or "").strip()
        ]
        if not targets or self.max_images <= 0:
            return document

        described = 0
        for index in targets:
            if described >= self.max_images:
                break
            block = document.blocks[index]
            located = self._locate(block, root, source_path, source_mime_type)
            if located is None:
                continue
            data, mime = located
            if len(data) > self.max_image_bytes:
                if self._is_source_image(source_path, source_mime_type):
                    # The upload itself is a picture: say so instead of
                    # returning a parse that silently contains nothing.
                    raise ImageTooLargeError(
                        "图片过大，暂不支持识别（上限 "
                        f"{self.max_image_bytes // (1024 * 1024)}MB）"
                    )
                logger.info(
                    "skipping oversized inline image in %s", source_path or root
                )
                continue
            description = self._describe(data, mime)
            if not description:
                continue
            document.blocks[index] = replace(
                block,
                text=description,
                metadata={**block.metadata, "description_source": "vision"},
            )
            described += 1
        return document

    def _locate(
        self,
        block,
        root: Path,
        source_path: Path | None,
        source_mime_type: str,
    ) -> tuple[bytes, str] | None:
        """The picture's bytes, from inside the parse output or the upload."""

        asset_ref = str(block.asset_ref or "").strip()
        if asset_ref:
            resolved_root = root.resolve()
            candidate = (resolved_root / asset_ref).resolve()
            if resolved_root in candidate.parents and candidate.is_file():
                mime = IMAGE_MIME_TYPES.get(
                    candidate.suffix.lower(), "image/png"
                )
                try:
                    return candidate.read_bytes(), mime
                except OSError:
                    return None
        if (
            source_path is not None
            and self._is_source_image(source_path, source_mime_type)
            and source_path.is_file()
        ):
            mime = str(source_mime_type or "").strip() or IMAGE_MIME_TYPES.get(
                source_path.suffix.lower(), "image/jpeg"
            )
            try:
                return source_path.read_bytes(), mime
            except OSError:
                return None
        return None

    @staticmethod
    def _is_source_image(source_path: Path | None, mime_type: str) -> bool:
        if str(mime_type or "").strip().lower().startswith("image/"):
            return True
        if source_path is None:
            return False
        return source_path.suffix.lower() in IMAGE_MIME_TYPES

    def _describe(self, data: bytes, mime: str) -> str:
        key = CACHE_KEY_PREFIX + hashlib.sha256(data).hexdigest()
        cached = self._cache_get(key)
        if cached:
            return cached
        try:
            description = self.vision.describe(data, mime)
        except VisionError as exc:
            logger.warning("vision description failed: %s", exc)
            return ""
        self._cache_set(key, description)
        return description

    def _cache_get(self, key: str) -> str:
        if self.cache is None:
            return ""
        try:
            raw = self.cache.get(key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not fail parsing
            logger.warning("vision cache read failed: %s", exc)
            return ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return ""
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("description") or "").strip()

    def _cache_set(self, key: str, description: str) -> None:
        if self.cache is None or self.cache_ttl_seconds <= 0:
            return
        payload = json.dumps(
            {
                "description": description,
                "model": str(getattr(self.vision, "model", "")),
            },
            ensure_ascii=False,
        )
        try:
            self.cache.setex(key, self.cache_ttl_seconds, payload)
        except Exception as exc:  # noqa: BLE001 - see _cache_get
            logger.warning("vision cache write failed: %s", exc)
