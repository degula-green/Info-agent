import json
import os
import tempfile
import unittest
from pathlib import Path

from services.collectors.wechat import service


class FakeDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [{"local_id": 7, "sort_seq": 7, "type": 3, "content": "image", "create_time": 1_700_000_000}]


class CollectorServiceTest(unittest.TestCase):
    def setUp(self):
        self.original = {name: getattr(service, name) for name in ("db", "knowledge", "download_attachment", "upload_attachment", "save_state")}
        service.binding.clear(); service.binding.update({"status": "running", "wxid": "wxid-test"})
        service.config.clear(); service.config.update({"enabled": True, "listen_mode": "whitelist", "selected_conversations": ["chat"], "connector_id": "account"})
        service.checkpoints.clear(); service.db = FakeDB(); self.calls = []

        def knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("assignments?connector_id=account"):
                return {"items": [{"collector": {"id": "collector", "status": "active"}, "conversation": {"external_conversation_id": "chat"}}]}
            if path.endswith("/messages"):
                return {"attachments": [{"id": "attachment", "content_status": "pending"}]}
            return {}

        def download(_chat_id, _raw):
            root = Path(tempfile.mkdtemp())
            path = root / "image.bin"; path.write_bytes(b"image bytes")
            return path, root

        service.knowledge = knowledge; service.download_attachment = download
        service.upload_attachment = lambda *args: self.calls.append(("upload", args, None)) or {}
        service.save_state = lambda: None

    def tearDown(self):
        for name, value in self.original.items(): setattr(service, name, value)

    def test_media_metadata_supports_image_file_and_video(self):
        self.assertEqual(service.normalized_type({"type": 3}), "image")
        self.assertEqual(service.normalized_type({"type": 49}), "file")
        self.assertEqual(service.normalized_type({"type": 43}), "file")
        self.assertEqual(service.media_type({"type": 43}), "video")
        self.assertEqual(service.attachment_metadata("chat", {"type": 43, "local_id": 9})[0]["file_name"], "video.mp4")

    def test_cursor_is_committed_after_attachment_upload(self):
        service.collect_once()
        operations = ["upload" if call[0] == "upload" else call[0].rsplit("/", 1)[-1] for call in self.calls]
        self.assertLess(operations.index("messages"), operations.index("upload"))
        self.assertLess(operations.index("upload"), operations.index("cursor"))
        self.assertEqual(service.checkpoints["collector"], 7)

    def test_upload_failure_does_not_commit_cursor(self):
        service.upload_attachment = lambda *args: (_ for _ in ()).throw(RuntimeError("upload failed"))
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            service.collect_once()
        self.assertFalse(any(call[0].endswith("/cursor") for call in self.calls))
        self.assertNotIn("collector", service.checkpoints)

    def test_local_state_only_restores_checkpoint_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "binding": {"wxid": "stale", "db_dir": "C:/stale", "status": "running"},
                "config": {"selected_conversations": ["stale-chat"]},
                "checkpoints": {"collector": 42},
            }), encoding="utf-8")
            original_path = os.environ.get("WECHAT_COLLECTOR_STATE_FILE")
            os.environ["WECHAT_COLLECTOR_STATE_FILE"] = str(path)
            try:
                service.binding.clear()
                service.config.clear()
                service.checkpoints.clear()
                service.load_state()
            finally:
                if original_path is None:
                    os.environ.pop("WECHAT_COLLECTOR_STATE_FILE", None)
                else:
                    os.environ["WECHAT_COLLECTOR_STATE_FILE"] = original_path

            self.assertEqual(service.binding, {})
            self.assertEqual(service.config, {})
            self.assertEqual(service.checkpoints, {"collector": 42})


if __name__ == "__main__":
    unittest.main()
