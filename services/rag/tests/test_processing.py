from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application.ports import ProcessingInput
from app.application.processing.chunking import build_chunks
from app.application.processing.preprocessor import DocumentPreprocessor, ProcessingError
from app.application.worker import RAGEventHandler
from app.application.ports import ProcessingOutput
from app.domain.models import AttachmentContext, CanonicalBlock, ParsedDocument
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.storage.artifacts import LocalArtifactStore, MinioArtifactStore, derived_key


class ProcessingTests(unittest.TestCase):
    def test_message_text_processing_does_not_create_file_metadata(self):
        context = AttachmentContext(
            attachment_id="message-placeholder", knowledge_item_id="message-1", file_name="", mime_type="",
            message_id="message-1", part_kind="message_display", content_access_required=False,
        )
        preprocessor = DocumentPreprocessor(artifact_store=LocalArtifactStore(Path(tempfile.mkdtemp()) / "artifacts"), embedding_provider=HashEmbeddingProvider())
        output = preprocessor.process_text("预计10月份进行招新", context, vectorize=False)
        self.assertEqual(output.attachment.attachment_id, None)
        self.assertIsNone(output.chunks[0].attachment_id)
        self.assertIsNone(output.chunks[0].file_name)
        self.assertEqual(output.chunks[0].part_kind, "message_display")
        self.assertNotIn("file_name", output.chunks[0].source_locator)
        self.assertNotIn("attachment_id", output.chunks[0].source_locator)

    def test_message_processing_strips_transport_attachment_metadata(self):
        context = AttachmentContext(
            attachment_id="transport-attachment", knowledge_item_id="message-2", file_name="temp.txt",
            mime_type="text/plain", part_kind="message_display",
            source_locator={"attachment_id": "transport-attachment", "file_name": "temp.txt", "message_id": "m-2"},
        )
        output = DocumentPreprocessor(
            artifact_store=LocalArtifactStore(Path(tempfile.mkdtemp()) / "artifacts"),
            embedding_provider=HashEmbeddingProvider(),
        ).process_text("普通消息正文", context, vectorize=False)
        self.assertEqual(output.chunks[0].source_locator, {"message_id": "m-2", "part_kind": "message_display", "paragraph_index": 0})

    def test_derived_artifact_key_stays_short_for_windows_paths(self) -> None:
        context = AttachmentContext(
            attachment_id="message-0ed8e84a-3993-4c54-b2b3-796e27fa82e5",
            file_name="message.txt",
            mime_type="text/plain",
            source_content_hash="sha256:" + "f" * 64,
        )
        key = derived_key(context, "436da7f9-3577-4f3b-bf1a-00233696aa4c", "manifest.json")
        self.assertLessEqual(len(key), 128)
        self.assertEqual(key, derived_key(context, "436da7f9-3577-4f3b-bf1a-00233696aa4c", "manifest.json"))

    def test_minio_store_accepts_inline_message_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "message.txt"
            destination = root / "copy" / "message.txt"
            source.write_text("message body", encoding="utf-8")
            context = AttachmentContext(
                attachment_id="message-1",
                file_name="message.txt",
                mime_type="text/plain",
                file_path=str(source),
            )
            store = MinioArtifactStore.__new__(MinioArtifactStore)
            self.assertEqual(store.download_source(context, destination), len("message body"))
            self.assertEqual(destination.read_text(encoding="utf-8"), "message body")

    def test_local_markdown_is_chunked_and_vectorized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "report.md"
            source.write_text("# 项目\n\n这是正文。", encoding="utf-8")
            context = AttachmentContext(
                attachment_id="att-1", knowledge_item_id="ki-1", file_name="report.md",
                mime_type="text/markdown", file_path=str(source), content_version=1,
                acl_version=3, part_kind="attachment_content",
            )
            output = DocumentPreprocessor(
                artifact_store=LocalArtifactStore(root / "artifacts"),
                embedding_provider=HashEmbeddingProvider(dimensions=1536),
            ).process(ProcessingInput(context, processing_job_id="job-1"))
            self.assertEqual(output.parsed.parser, "local-md")
            self.assertEqual(len(output.chunks), 1)
            self.assertEqual(len(output.chunks[0].embedding or []), 1536)
            self.assertEqual(output.chunks[0].auth_object_key, "attachment_content:att-1")

    def test_protected_attachment_is_marked_for_protected_index(self) -> None:
        parsed = ParsedDocument(
            markdown="secret text",
            blocks=[CanonicalBlock(None, 0, "text", "secret text")],
            parser="fixture", parser_version="1",
        )
        context = AttachmentContext(
            attachment_id="att-2", knowledge_item_id="ki-2", file_name="secret.txt",
            mime_type="text/plain", content_access_required=True, acl_version=4,
            part_kind="attachment_content",
        )
        chunks = build_chunks(parsed, context)
        self.assertTrue(chunks[0].protected)
        self.assertEqual(chunks[0].auth_object_key, "attachment_content:att-2")
        self.assertEqual(chunks[0].content_visibility, "protected")

    def test_string_false_access_flag_is_not_treated_as_true(self) -> None:
        context = AttachmentContext.from_mapping({"attachment_id": "a", "file_name": "a.txt", "mime_type": "text/plain", "content_access_required": "false"})
        self.assertFalse(context.content_access_required)

    def test_metadata_chunk_is_not_vectorized(self) -> None:
        context = AttachmentContext(
            attachment_id="att-3", file_name="confidential.pdf", mime_type="application/pdf",
            part_kind="attachment_metadata",
        )
        chunks = build_chunks(ParsedDocument("", [], "fixture", "1"), context)
        self.assertEqual(len(chunks), 1)
        self.assertFalse(chunks[0].rag_eligible)
        self.assertEqual(chunks[0].auth_object_key, "attachment_meta:att-3")

    def test_quality_review_does_not_index_replacement_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.txt"
            source.write_text("bad \ufffd text", encoding="utf-8")
            context = AttachmentContext(attachment_id="att-4", file_name="bad.txt", mime_type="text/plain", file_path=str(source))
            with self.assertRaises(ProcessingError) as error:
                DocumentPreprocessor(artifact_store=LocalArtifactStore(root / "artifacts"), embedding_provider=HashEmbeddingProvider()).process(ProcessingInput(context))
            self.assertEqual(error.exception.code, "QUALITY_REVIEW_REQUIRED")

    def test_worker_is_idempotent_and_publishes_completion(self) -> None:
        class FakePreprocessor:
            def process(self, item, *, vectorize=True):
                return ProcessingOutput(item.attachment, ParsedDocument("ok", [], "fixture", "1"), [], status="succeeded")

        class FakeIndexer:
            def index_chunks(self, chunks):
                return len(chunks)

        class FakePublisher:
            def __init__(self): self.events = []
            def publish(self, value): self.events.append(value); return "1-0"

        from app.infrastructure.persistence.repository import InMemoryRagRepository
        repo = InMemoryRagRepository()
        publisher = FakePublisher()
        handler = RAGEventHandler(preprocessor=FakePreprocessor(), indexer=FakeIndexer(), repository=repo, publisher=publisher, knowledge=object())
        event = {"event_id": "evt-1", "event_type": "knowledge.ready", "schema_version": 1, "occurred_at": "now", "trace_id": "trace-1", "producer": "module-2", "organization_id": None, "payload": {"knowledge_item_id": "ki-1", "attachment": {"attachment_id": "att-1", "file_name": "a.txt", "mime_type": "text/plain", "file_path": "fixture.txt"}}}
        handler.handle(event)
        handler.handle(event)
        self.assertEqual(len(publisher.events), 1)
        self.assertEqual(len(repo.processing_jobs), 1)
