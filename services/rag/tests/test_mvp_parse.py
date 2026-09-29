from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path

from app.application.parse_service import MVPParseService
from app.domain.rag import ResourceContext
from app.infrastructure.storage.artifacts import LocalArtifactStore


ITEM = "00000000-0000-0000-0000-000000000002"
MESSAGE = "00000000-0000-0000-0000-000000000001"
SCOPE = "00000000-0000-0000-0000-000000000003"
KB = "00000000-0000-0000-0000-000000000004"


class _Knowledge:
    def __init__(self, *, content_access_required=False, display_text="display text", original_text="secret original"):
        self.content_access_required = content_access_required
        self.display_text = display_text
        self.original_text = original_text

    def get_knowledge(self, knowledge_item_id, **kwargs):
        return {
            "knowledge_item_id": ITEM,
            "resource_type": "message",
            "resource_id": MESSAGE,
            "knowledge_base_id": KB,
            "scope_type": "organization",
            "scope_id": SCOPE,
            "content_version": 1,
            "acl_version": 1,
            "content_hash": "a" * 64,
            "content_access_required": self.content_access_required,
        }

    def get_content(self, knowledge_item_id, **kwargs):
        variant = kwargs.get("content_variant") or "display"
        return {
            "text": self.original_text if variant == "original" else self.display_text,
        }


class _Artifacts:
    def download_source(self, context, path):
        Path(path).write_bytes(Path(context.object_ref).read_bytes())


class ParseTests(unittest.TestCase):
    def test_artifact_store_uses_object_ref_without_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "downloads" / "source.txt"
            source.write_text("stored object", encoding="utf-8")
            context = ResourceContext(
                knowledge_item_id=ITEM,
                resource_type="attachment",
                resource_id=MESSAGE,
                knowledge_base_id=KB,
                scope_type="organization",
                scope_id=SCOPE,
                content_version=1,
                content_hash="a" * 64,
                object_ref=str(source),
            )
            store = LocalArtifactStore(root / "artifacts")
            size = store.download_source(context, destination)
            self.assertEqual(size, source.stat().st_size)
            self.assertEqual(destination.read_text(encoding="utf-8"), "stored object")

    def test_protected_message_creates_display_and_protected_chunks(self) -> None:
        service = MVPParseService(
            knowledge=_Knowledge(content_access_required=True),
            artifact_store=_Artifacts(),
        )
        result = service.run(
            {
                "knowledge_item_id": ITEM,
                "resource_type": "message",
                "resource_id": MESSAGE,
                "content_version": 1,
                "acl_version": 1,
                "source_conversation_id": None,
            },
            {
                "resource_type": "message",
                "resource_id": MESSAGE,
                "knowledge_item_id": ITEM,
                "content_version": 1,
                "acl_version": 1,
            },
        )
        self.assertEqual({chunk.content_variant for chunk in result.chunks}, {"display", "protected"})
        self.assertEqual({chunk.logical_position_key for chunk in result.chunks}.__len__(), 1)

    def test_unsupported_attachment_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unsupported.zip"
            source.write_bytes(b"PK\x03\x04")
            knowledge = _Knowledge()
            knowledge.get_knowledge = lambda *args, **kwargs: {
                "knowledge_item_id": ITEM,
                "resource_type": "attachment",
                "resource_id": MESSAGE,
                "knowledge_base_id": KB,
                "scope_type": "organization",
                "scope_id": SCOPE,
                "content_version": 1,
                "acl_version": 1,
                "content_hash": "a" * 64,
                "content_access_required": False,
                "object_ref": str(source),
                "file_name": "unsupported.zip",
                "mime_type": "application/zip",
            }
            service = MVPParseService(
                knowledge=knowledge,
                artifact_store=_Artifacts(),
            )
            result = service.run(
                {
                    "knowledge_item_id": ITEM,
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "content_version": 1,
                    "acl_version": 1,
                    "source_conversation_id": None,
                },
                {
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "knowledge_item_id": ITEM,
                    "content_version": 1,
                    "acl_version": 1,
                },
            )
            self.assertEqual(result.status, "metadata_only")
            self.assertFalse(result.chunks)

    def test_supported_text_attachment_becomes_a_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes.txt"
            source.write_text("attachment marker for rag", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            knowledge = _Knowledge()
            knowledge.get_knowledge = lambda *args, **kwargs: {
                "knowledge_item_id": ITEM,
                "resource_type": "attachment",
                "resource_id": MESSAGE,
                "knowledge_base_id": KB,
                "scope_type": "organization",
                "scope_id": SCOPE,
                "content_version": 1,
                "acl_version": 1,
                "content_hash": digest,
                "content_access_required": False,
                "object_ref": str(source),
                "file_name": "notes.txt",
                "mime_type": "text/plain",
            }
            service = MVPParseService(knowledge=knowledge, artifact_store=_Artifacts())
            result = service.run(
                {
                    "knowledge_item_id": ITEM,
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "content_version": 1,
                    "acl_version": 1,
                    "content_hash": digest,
                    "source_conversation_id": None,
                },
                {
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "knowledge_item_id": ITEM,
                    "content_version": 1,
                    "acl_version": 1,
                },
            )
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.chunks), 1)
            self.assertIn("attachment marker", result.chunks[0].content)

    def test_attachment_object_ref_is_resolved_from_nested_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "faq.txt"
            source.write_text("nested attachment metadata", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            attachment_id = "00000000-0000-0000-0000-000000000005"
            knowledge = _Knowledge()
            knowledge.get_knowledge = lambda *args, **kwargs: {
                "knowledge_item_id": ITEM,
                "resource_type": "attachment",
                "resource_id": MESSAGE,
                "knowledge_base_id": KB,
                "scope_type": "organization",
                "scope_id": SCOPE,
                "content_version": 1,
                "acl_version": 1,
                "content_hash": digest,
                "content_access_required": False,
                "attachments": [
                    {
                        "attachment_id": attachment_id,
                        "object_ref": str(source),
                        "file_name": "faq.txt",
                        "mime_type": "text/plain",
                        "size_bytes": source.stat().st_size,
                    }
                ],
            }
            service = MVPParseService(knowledge=knowledge, artifact_store=_Artifacts())
            result = service.run(
                {
                    "knowledge_item_id": ITEM,
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "content_version": 1,
                    "acl_version": 1,
                    "content_hash": digest,
                    "source_attachment_id": attachment_id,
                    "source_conversation_id": None,
                },
                {
                    "resource_type": "attachment",
                    "resource_id": MESSAGE,
                    "knowledge_item_id": ITEM,
                    "content_version": 1,
                    "acl_version": 1,
                },
            )
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.context.object_ref, str(source))
            self.assertEqual(len(result.chunks), 1)
            self.assertIn("nested attachment metadata", result.chunks[0].content)


if __name__ == "__main__":
    unittest.main()
