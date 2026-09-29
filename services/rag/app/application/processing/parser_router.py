from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.application.processing.preflight import PreflightResult
from app.config import settings
from app.domain.models import ParsedDocument
from app.domain.rag import ResourceContext
from app.infrastructure.parsing.local import LocalDocumentParser
from app.infrastructure.parsing.mineru import (
    MinerUClient,
    MinerUResultNormalizer,
    safe_extract_zip,
)


@dataclass(frozen=True)
class ParserRouter:
    local: LocalDocumentParser
    mineru: MinerUClient
    normalizer: MinerUResultNormalizer

    def parse(
        self,
        preflight: PreflightResult,
        context: ResourceContext,
        root: Path,
    ) -> ParsedDocument:
        if preflight.category == "local":
            return self.local.parse(preflight.path, preflight.extension)
        try:
            if (
                settings.mineru_submit_mode == "url"
                and context.object_ref
                and context.object_ref.startswith(("http://", "https://"))
            ):
                task_id = self.mineru.submit_by_url(
                    context.object_ref,
                    context.file_name or "",
                    data_id=preflight.sha256,
                )
            else:
                task_id = self.mineru.submit_by_upload(
                    preflight.path,
                    data_id=preflight.sha256,
                )
            response = self.mineru.poll(task_id)
            archive = root / "result.zip"
            self.mineru.download_result(response, archive)
            extracted = root / "result"
            safe_extract_zip(archive, extracted)
            return self.normalizer.normalize(extracted)
        except Exception:
            if preflight.extension == "docx":
                return self.local.parse_docx(preflight.path)
            raise
