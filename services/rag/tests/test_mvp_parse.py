from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application.parse_service import MVPParseService


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
        Path(path).write_bytes(b"not really a zip")


class ParseTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
