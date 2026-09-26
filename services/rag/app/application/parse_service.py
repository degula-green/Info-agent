from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.application.mvp_ports import KnowledgeSource
from app.application.processing.mvp_chunking import build_variant_chunks, parsed_from_text
from app.application.processing.preflight import PreflightError, validate_attachment
from app.application.processing.preprocessor import ParserRouter
from app.application.processing.quality import QualityChecker
from app.config import settings
from app.domain.models import ParsedDocument
from app.domain.rag import Chunk, ResourceContext
from app.infrastructure.parsing.local import LocalDocumentParser, LocalParseError
from app.infrastructure.parsing.mineru import MinerUClient, MinerUResultNormalizer
from app.infrastructure.storage.artifacts import ArtifactStore, StorageError


class ParseStageError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class ParseResult:
    context: ResourceContext
    snapshot_id: str
    chunks: list[Chunk]
    status: str
    skip_reason: str | None = None
    parser: str | None = None


METADATA_ONLY_CODES = {
    "UNSUPPORTED_FORMAT",
    "FILE_TOO_LARGE",
    "EMPTY_FILE",
    "PASSWORD_PROTECTED",
    "PAGE_LIMIT_EXCEEDED",
    "MIME_EXTENSION_MISMATCH",
    "MAGIC_MISMATCH",
    "SIZE_MISMATCH",
}


class MVPParseService:
    def __init__(
        self,
        *,
        knowledge: KnowledgeSource,
        artifact_store: ArtifactStore,
        parser_router: ParserRouter | None = None,
        quality_checker: QualityChecker | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.artifact_store = artifact_store
        self.parser_router = parser_router or ParserRouter(
            LocalDocumentParser(), MinerUClient(), MinerUResultNormalizer()
        )
        self.quality_checker = quality_checker or QualityChecker()

    def run(self, job: dict[str, Any], envelope_payload: dict[str, Any]) -> ParseResult:
        metadata = self.knowledge.get_knowledge(
            job["knowledge_item_id"],
            content_version=job["content_version"],
            acl_version=job["acl_version"],
            purpose="index",
        )
        context = ResourceContext.from_event_and_source(envelope_payload, metadata)
        if context.knowledge_item_id != job["knowledge_item_id"]:
            raise ParseStageError("SOURCE_IDENTITY_MISMATCH", "Knowledge returned a different item")
        if context.content_version != int(job["content_version"]):
            raise ParseStageError("SOURCE_VERSION_MISMATCH", "content_version changed")
        if context.acl_version != int(job["acl_version"]):
            raise ParseStageError("SOURCE_ACL_VERSION_MISMATCH", "acl_version changed")
        if (
            job.get("source_conversation_id")
            and context.source_conversation_id
            and str(job["source_conversation_id"]) != str(context.source_conversation_id)
        ):
            raise ParseStageError("SOURCE_CONVERSATION_MISMATCH", "source conversation changed")
        if context.resource_type != job["resource_type"] or context.resource_id != job["resource_id"]:
            raise ParseStageError("SOURCE_RESOURCE_MISMATCH", "Knowledge returned a different resource")
        if context.resource_type == "message":
            return self._parse_message(context)
        return self._parse_attachment(context)

    def _parse_message(self, context: ResourceContext) -> ParseResult:
        display = self.knowledge.get_content(
            context.knowledge_item_id,
            content_version=context.content_version,
            acl_version=context.acl_version,
            content_variant="display",
            purpose="index",
        )
        display_text = _content_text(display)
        if not display_text.strip():
            return ParseResult(context, "", [], "metadata_only", "empty_content")
        parsed = parsed_from_text(display_text)
        chunks = build_variant_chunks(
            parsed,
            context,
            snapshot_id="",
            variant="display",
            processing_version=settings.processing_version,
            chunking_version=settings.chunking_version,
        )
        if context.content_access_required:
            original = self.knowledge.get_content(
                context.knowledge_item_id,
                content_version=context.content_version,
                acl_version=context.acl_version,
                content_variant="original",
                purpose="index",
            )
            original_text = _content_text(original)
            if original_text.strip():
                protected_chunks = build_variant_chunks(
                    parsed_from_text(original_text),
                    context,
                    snapshot_id="",
                    variant="protected",
                    processing_version=settings.processing_version,
                    chunking_version=settings.chunking_version,
                )
                chunks.extend(protected_chunks)
        return ParseResult(context, "", chunks, "succeeded", parser=parsed.parser)

    def _parse_attachment(self, context: ResourceContext) -> ParseResult:
        if not context.object_ref:
            return ParseResult(context, "", [], "metadata_only", "object_ref_missing")
        run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", uuid.uuid4().hex)[:128]
        work_dir = settings.preprocess_work_dir / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            source_path = work_dir / Path(context.file_name or context.resource_id).name
            try:
                self.artifact_store.download_source(context, source_path)
            except StorageError as exc:
                raise ParseStageError("SOURCE_FETCH_FAILED", str(exc), retryable=True) from exc
            try:
                preflight = validate_attachment(source_path, context)
            except PreflightError as exc:
                if exc.code in METADATA_ONLY_CODES:
                    return ParseResult(context, "", [], "metadata_only", exc.code.lower())
                raise ParseStageError(exc.code, str(exc), retryable=False) from exc
            try:
                parsed = self.parser_router.parse(preflight, context, work_dir)
            except LocalParseError as exc:
                return ParseResult(context, "", [], "metadata_only", exc.code.lower())
            if parsed.markdown.strip() and not parsed.blocks:
                parsed = parsed_from_text(parsed.markdown, parser=parsed.parser)
            quality = self.quality_checker.check(parsed)
            if quality.status == "failed":
                return ParseResult(context, "", [], "metadata_only", "quality_failed")
            variants: list[tuple[str, ParsedDocument]] = []
            if context.content_access_required:
                variants.append(("protected", parsed))
                display_text = _display_text_from_source(context)
                if display_text.strip():
                    variants.append(("display", parsed_from_text(display_text)))
            else:
                variants.append(("display", parsed))
            chunks: list[Chunk] = []
            for variant, variant_document in variants:
                chunks.extend(build_variant_chunks(
                    variant_document,
                    context,
                    snapshot_id="",
                    variant=variant,
                    processing_version=settings.processing_version,
                    chunking_version=settings.chunking_version,
                ))
            if not chunks:
                return ParseResult(context, "", [], "metadata_only", "no_searchable_text")
            return ParseResult(context, "", chunks, "succeeded", parser=parsed.parser)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def attach_snapshot(chunks: list[Chunk], snapshot_id: str) -> list[Chunk]:
    for chunk in chunks:
        chunk.resource_snapshot_id = snapshot_id
    return chunks


def _content_text(value: dict[str, Any]) -> str:
    for key in ("text", "content", "body"):
        if value.get(key) is not None:
            return str(value[key])
    return ""


def _display_text_from_source(context: ResourceContext) -> str:
    for key in ("display_text", "masked_text", "display_content"):
        value = context.raw.get(key)
        if isinstance(value, str):
            return value
    return ""
