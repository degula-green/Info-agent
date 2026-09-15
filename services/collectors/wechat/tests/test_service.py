import json
import os
import tempfile
import unittest
from pathlib import Path

from services.collectors.wechat import service


class FakeDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [{"local_id": 7, "sort_seq": 7, "type": 3, "content": "image", "create_time": 1_700_000_000}]


class IncrementalFallbackDB:
    def get_new_messages(self, _chat_id, since_seq=0, limit=200):
        raise RuntimeError("database disk image is malformed")

    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [
            {"local_id": 9, "sort_seq": 9, "type": "文本", "content": "newer", "create_time": 1_700_000_009},
            {"local_id": 8, "sort_seq": 8, "type": "文本", "content": "new", "create_time": 1_700_000_008},
            {"local_id": 3, "sort_seq": 3, "type": "文本", "content": "old", "create_time": 1_700_000_003},
        ]


class CollectorServiceTest(unittest.TestCase):
    def setUp(self):
        self.original = {name: getattr(service, name) for name in ("db", "knowledge", "download_attachment", "upload_attachment", "save_state")}
        service.binding.clear(); service.binding.update({"status": "running", "wxid": "wxid-test"})
        service.config.clear(); service.config.update({"enabled": True, "listen_mode": "whitelist", "selected_conversations": ["chat"], "connector_id": "account"})
        service.checkpoints.clear(); service.db = FakeDB(); self.calls = []

        def knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("assignments?connector_id=account"):
                return {"items": [{"collector": {"id": "collector", "status": "active"}, "conversation": {"status": "active", "external_conversation_id": "chat"}}]}
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
        self.assertEqual(service.normalized_type({"type": 49}), "text")
        self.assertEqual(service.normalized_type({"type": 43}), "file")
        self.assertEqual(service.normalized_type({"type": "视频"}), "file")
        self.assertEqual(service.media_type({"type": 43}), "video")
        self.assertEqual(service.media_type({"type": "视频"}), "video")
        self.assertEqual(service.attachment_metadata("chat", {"type": 43, "local_id": 9})[0]["file_name"], "video.mp4")

    def test_incremental_read_falls_back_when_wechat_shard_is_rewritten(self):
        rows = service.messages_after(IncrementalFallbackDB(), "chat", since=3, limit=10)
        self.assertEqual([row["sort_seq"] for row in rows], [8, 9])

    def test_media_payload_overrides_lossy_database_type(self):
        image = '<msg><img fromusername="wxid_image" length="12" /></msg>'
        file = '<msg><appmsg><type>6</type><title>note.pdf</title></appmsg></msg>'
        self.assertEqual(service.normalized_type({"type": "文本", "content": image}), "image")
        self.assertEqual(service.media_type({"type": "文本", "content": image}), "image")
        self.assertEqual(service.normalized_type({"type": "文本", "content": file}), "file")
        self.assertEqual(service.media_type({"type": "文本", "content": file}), "file")
        self.assertEqual(service.resolve_sender({"type": "文本", "content": image}, {})[0], "wxid_image")

    def test_link_cards_and_forwarded_records_are_not_files(self):
        link = '<msg><appmsg><title>WPS 云文档</title><type>33</type><appattach><datasize>743898</datasize></appattach></appmsg></msg>'
        forwarded = '<msg><appmsg><title>聊天记录</title><type>19</type><appattach><datasize>743898</datasize></appattach></msg>'
        real_file = '<msg><appmsg><title>report.xls</title><type>6</type><appattach><fileext>xls</fileext><totallen>12</totallen></appattach></msg>'
        self.assertEqual(service.normalized_type({"type": "文件/链接/卡片", "content": link}), "text")
        self.assertEqual(service.normalized_type({"type": "文件/链接/卡片", "content": forwarded}), "text")
        self.assertEqual(service.normalized_type({"type": "文件/链接/卡片", "content": real_file}), "file")
        self.assertEqual(service.normalized_type({"type": 49, "content": link}), "text")
        self.assertEqual(service.normalized_type({"type": 49, "content": forwarded}), "text")
        self.assertEqual(service.normalized_type({"type": 49, "content": real_file}), "file")
        self.assertEqual(service.media_type({"type": 49, "content": forwarded}), "")
        self.assertEqual(service.media_type({"type": 49, "content": real_file}), "file")
        self.assertEqual(service.attachment_metadata("chat", {"type": "文件/链接/卡片", "local_id": 10, "content": forwarded}), [])
        self.assertEqual(service.attachment_metadata("chat", {"type": "文件/链接/卡片", "local_id": 11, "content": real_file})[0]["mime_type"], "application/vnd.ms-excel")

    def test_nested_forwarded_file_payload_is_classified_and_named(self):
        nested = (
            '<msg><appmsg><type>57</type><refermsg><content>'
            '&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;6&lt;/type&gt;'
            '&lt;title&gt;勤工助学岗位汇总表(10).xls&lt;/title&gt;'
            '&lt;appattach&gt;&lt;totallen&gt;2048&lt;/totallen&gt;'
            '&lt;fileext&gt;xls&lt;/fileext&gt;&lt;/appattach&gt;&lt;/appmsg&gt;&lt;/msg&gt;'
            '</content></refermsg></appmsg></msg>'
        )
        raw = {"type": 57, "local_id": 12, "content": nested}
        self.assertEqual(service.normalized_type(raw), "file")
        self.assertEqual(service.media_type(raw), "file")
        metadata = service.attachment_metadata("chat", raw)
        self.assertEqual(metadata[0]["file_name"], "勤工助学岗位汇总表(10).xls")
        self.assertEqual(metadata[0]["mime_type"], "application/vnd.ms-excel")
        self.assertEqual(metadata[0]["size_bytes"], 2048)

    def test_sender_prefix_is_removed_without_changing_normal_colons(self):
        self.assertEqual(service.strip_sender_prefix("wxid_sender: hello", "wxid_sender"), "hello")
        self.assertEqual(service.strip_sender_prefix("wxid_sender@chatroom: <?xml version='1.0'?>", "wxid_sender"), "<?xml version='1.0'?>")
        self.assertEqual(service.strip_sender_prefix("Note: keep this", "wxid_sender"), "Note: keep this")

    def test_group_sender_prefers_content_prefix_and_xml_sender(self):
        names = {"wxid_real": "真实成员", "wxid_xml": "XML成员"}
        sender, display = service.resolve_sender({"sender_username": "wxid_wrong", "content": "wxid_real:\\nhello"}, names)
        self.assertEqual((sender, display), ("wxid_real", "真实成员"))
        sender, display = service.resolve_sender({"sender_username": "wxid_wrong", "content": "<fromusername>wxid_xml</fromusername>"}, names)
        self.assertEqual((sender, display), ("wxid_xml", "XML成员"))

    def test_local_file_fallback_matches_wechat_duplicate_name(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / "account"
            file_dir = account / "msg" / "file" / "2026-09"
            file_dir.mkdir(parents=True)
            duplicate = file_dir / "report(1).xlsx"
            duplicate.write_bytes(b"xlsx")
            original_db = service.db
            try:
                service.db = type("FileDB", (), {"account_dir": str(account)})()
                raw = {"type": "文件/链接/卡片", "create_time": 1789082295, "content": "<msg><appmsg><title>report.xlsx</title><type>6</type><appattach><fileext>xlsx</fileext></appattach></appmsg></msg>"}
                target = Path(directory) / "downloaded.xlsx"
                copied = service.local_file_fallback(raw, target.parent)
                self.assertEqual(Path(copied).read_bytes(), b"xlsx")
            finally:
                service.db = original_db

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

    def test_missing_local_attachment_does_not_wedge_cursor(self):
        service.download_attachment = lambda *_args: None
        service.collect_once()
        self.assertTrue(any(call[0].endswith("/cursor") for call in self.calls))
        self.assertEqual(service.checkpoints["collector"], 7)
        self.assertNotIn("last_error", service.binding)

    def test_paused_conversation_is_not_collected(self):
        self.calls.clear()
        original_knowledge = service.knowledge

        def paused_knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("assignments?connector_id=account"):
                return {"items": [{"collector": {"id": "collector", "status": "active"}, "conversation": {"status": "paused", "external_conversation_id": "chat"}}]}
            return {}

        service.knowledge = paused_knowledge
        try:
            service.collect_once()
        finally:
            service.knowledge = original_knowledge
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in self.calls], ["assignments?connector_id=account"])

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
