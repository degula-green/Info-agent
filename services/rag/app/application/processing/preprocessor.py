from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.application.ports import EmbeddingProvider, ProcessingInput, ProcessingOutput
from app.application.processing.chunking import build_chunks
from app.application.processing.preflight import PreflightError, PreflightResult, validate_attachment
from app.application.processing.quality import QualityChecker
from app.application.processing.vectorization import VectorizationError, vectorize_chunks
from app.config import settings
from app.domain.models import AttachmentContext, CanonicalBlock, ParsedDocument
from app.infrastructure.parsing.local import LocalDocumentParser, LocalParseError
from app.infrastructure.parsing.mineru import MinerUClient, MinerUResultNormalizer, safe_extract_zip
from app.infrastructure.storage.artifacts import ArtifactStore, StorageError, derived_key


class ProcessingError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ParserRouter:
    local: LocalDocumentParser
    mineru: MinerUClient
    normalizer: MinerUResultNormalizer

    def parse(self, preflight: PreflightResult, context: AttachmentContext, root: Path) -> ParsedDocument:
        if preflight.category == "local":
            return self.local.parse(preflight.path, preflight.extension)
        try:
            if settings.mineru_submit_mode == "url" and context.object_ref and context.object_ref.startswith(("http://", "https://")):
                task_id = self.mineru.submit_by_url(context.object_ref, context.file_name, data_id=preflight.sha256)
            else:
                task_id = self.mineru.submit_by_upload(preflight.path, data_id=preflight.sha256)
            response = self.mineru.poll(task_id)
            archive = root / "result.zip"
            self.mineru.download_result(response, archive)
            extracted = root / "result"
            safe_extract_zip(archive, extracted)
            return self.normalizer.normalize(extracted)
        except Exception as exc:
            if preflight.extension == "docx":
                # DOCX is XML-based and remains searchable when the remote
                # layout parser is unavailable. Keep MinerU as the preferred
                # path, but avoid making a transient cloud outage fatal.
                return self.local.parse_docx(preflight.path)
            if isinstance(exc, ProcessingError):
                raise
            retryable = bool(getattr(exc, "retryable", False))
            raise ProcessingError("MINERU_FAILED", "MinerU document processing failed", retryable=retryable) from exc


class DocumentPreprocessor:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        embedding_provider: EmbeddingProvider,
        parser_router: ParserRouter | None = None,
        quality_checker: QualityChecker | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.embedding_provider = embedding_provider
        self.parser_router = parser_router or ParserRouter(LocalDocumentParser(), MinerUClient(), MinerUResultNormalizer())
        self.quality_checker = quality_checker or QualityChecker()

    def process(self, item: ProcessingInput, *, vectorize: bool = True) -> ProcessingOutput:
        context = item.attachment
        raw_run_id = item.processing_job_id or uuid.uuid4().hex
        run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw_run_id))[:128] or uuid.uuid4().hex
        work_dir = settings.preprocess_work_dir / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            source_path = work_dir / Path(context.file_name).name
            try:
                self.artifact_store.download_source(context, source_path)
            except StorageError as exc:
                raise ProcessingError("SOURCE_FETCH_FAILED", str(exc), retryable=True) from exc
            preflight = validate_attachment(source_path, context)
            # Use the verified source digest for immutable derived paths even
            # when an upstream event omitted source_content_hash.
            context = replace(context, source_content_hash=f"sha256:{preflight.sha256}")
            try:
                parsed = self.parser_router.parse(preflight, context, work_dir)
            except LocalParseError as exc:
                raise ProcessingError(exc.code, str(exc), retryable=False) from exc
            if parsed.markdown.strip() and not parsed.blocks:
                parsed.blocks = [CanonicalBlock(None, 0, "text", parsed.markdown.strip())]
            quality = self.quality_checker.check(parsed)
            if quality.status == "failed":
                raise ProcessingError("QUALITY_FAILED", "parser returned no usable searchable content")
            parsed_hash = self._artifact_hash(parsed)
            manifest = self._manifest(context, preflight, parsed, parsed_hash, run_id, quality.status, quality.flags)
            artifact_ref = self._save_artifacts(context, parsed, manifest, run_id, work_dir)
            parsed.artifact_ref = artifact_ref
            parsed.manifest = manifest
            if quality.status != "passed":
                raise ProcessingError("QUALITY_REVIEW_REQUIRED", "parsed artifact requires quality review")
            chunks = build_chunks(parsed, context)
            if not chunks and context.part_kind == "attachment_content":
                metadata_context = replace(context, part_kind="attachment_metadata", content_access_required=False)
                chunks = build_chunks(ParsedDocument("", [], parsed.parser, parsed.parser_version), metadata_context)
            if vectorize:
                try:
                    vectorize_chunks(chunks, self.embedding_provider)
                except VectorizationError as exc:
                    raise ProcessingError("EMBEDDING_FAILED", str(exc), retryable=True) from exc
            return ProcessingOutput(context, parsed, chunks, status="succeeded")
        except PreflightError as exc:
            raise ProcessingError(exc.code, str(exc), retryable=False) from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def process_text(self, text: str, context: AttachmentContext, *, vectorize: bool = True) -> ProcessingOutput:
        """Process a message body without creating a synthetic file artifact."""
        value = str(text or "").strip()
        # A message may arrive with transport-level attachment metadata. It is
        # not part of the message source identity and must not leak into the
        # searchable Chunk metadata.
        message_source_locator = {
            key: item for key, item in context.source_locator.items()
            if key not in {"attachment_id", "file_name", "mime_type", "file_path", "storage_key"}
        }
        message_context = replace(
            context,
            file_name="",
            mime_type="",
            attachment_id=None,
            part_kind="message_display",
            source_locator=message_source_locator,
        )
        parsed = ParsedDocument(
            markdown=value,
            blocks=[CanonicalBlock(None, 0, "text", value)] if value else [],
            parser="inline-text",
            parser_version="v1",
        )
        quality = self.quality_checker.check(parsed)
        if quality.status == "failed":
            raise ProcessingError("QUALITY_FAILED", "message returned no usable searchable content")
        chunks = build_chunks(parsed, message_context)
        if vectorize:
            try:
                vectorize_chunks(chunks, self.embedding_provider)
            except VectorizationError as exc:
                raise ProcessingError("EMBEDDING_FAILED", str(exc), retryable=True) from exc
        return ProcessingOutput(message_context, parsed, chunks, status="succeeded")

    def _manifest(self, context: AttachmentContext, preflight: PreflightResult, parsed: ParsedDocument, parsed_hash: str, run_id: str, quality_status: str, quality_flags: tuple[str, ...]) -> dict[str, Any]:
        parser_options = self._parser_options(preflight)
        return {
            "artifact_schema_version": 1,
            "attachment_id": context.attachment_id,
            "knowledge_item_id": context.knowledge_item_id,
            "content_version": context.content_version,
            "acl_version": context.acl_version,
            "source_content_hash": f"sha256:{preflight.sha256}",
            "file_name": context.file_name,
            "mime_type": preflight.mime_type,
            "size_bytes": preflight.size_bytes,
            "page_count": preflight.page_count,
            "parser": parsed.parser,
            "parser_version": parsed.parser_version,
            "model_version": settings.mineru_model_version if preflight.category == "mineru" else None,
            "backend": preflight.category,
            "parser_options": parser_options,
            "parser_options_hash": hashlib.sha256(json.dumps(parser_options, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "artifact_hash": f"sha256:{parsed_hash}",
            "block_count": len(parsed.blocks),
            "asset_count": len(parsed.asset_refs),
            "quality_flags": list(preflight.warnings) + list(quality_flags),
            "status": quality_status,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_artifacts(self, context: AttachmentContext, parsed: ParsedDocument, manifest: dict[str, Any], run_id: str, work_dir: Path) -> str:
        self.artifact_store.put_text(derived_key(context, run_id, "full.md"), parsed.markdown)
        self.artifact_store.put_json(derived_key(context, run_id, "content_list.json"), {"blocks": [block.as_dict() for block in parsed.blocks]})
        self.artifact_store.put_json(derived_key(context, run_id, "manifest.json"), manifest)
        for asset_ref in parsed.asset_refs:
            relative = Path(str(asset_ref).replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            candidates = (work_dir / "result" / relative, work_dir / relative)
            source = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
            if source is None:
                continue
            self.artifact_store.put_bytes(derived_key(context, run_id, str(Path("assets") / relative)), source.read_bytes())
        for name, reference in parsed.auxiliary_files.items():
            relative = Path(str(reference).replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = (work_dir / "result" / relative).resolve()
            result_root = (work_dir / "result").resolve()
            if result_root not in source.parents or not source.is_file():
                continue
            self.artifact_store.put_bytes(derived_key(context, run_id, name), source.read_bytes(), "application/json")
        return derived_key(context, run_id, "manifest.json")

    @staticmethod
    def _artifact_hash(parsed: ParsedDocument) -> str:
        value = {
            "markdown": parsed.markdown,
            "blocks": [block.as_dict() for block in parsed.blocks],
            "assets": sorted(parsed.asset_refs),
        }
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _parser_options(preflight: PreflightResult) -> dict[str, Any]:
        if preflight.category != "mineru":
            return {"format": preflight.extension, "parser": "local-v1"}
        return {
            "format": preflight.extension,
            "model_version": settings.mineru_model_version,
            "is_ocr": settings.mineru_enable_ocr,
            "enable_formula": settings.mineru_enable_formula,
            "enable_table": settings.mineru_enable_table,
            "language": settings.mineru_language,
            "submit_mode": settings.mineru_submit_mode,
        }
