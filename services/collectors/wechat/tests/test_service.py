import json
import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from services.collectors.wechat import service


class FakeDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [{"local_id": 7, "sort_seq": 7, "type": 3, "content": "image", "create_time": 1_700_000_000}]


class DeviceCredentialRetentionTests(unittest.TestCase):
    @staticmethod
    def http_error(code: int, payload: dict):
        return urllib.error.HTTPError(
            "http://knowledge.local",
            code,
            "test error",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )

    def test_terminal_device_error_clears_pairing(self):
        error = self.http_error(401, {"code": "agent_device_expired"})
        self.assertTrue(service.should_clear_device_credentials(error))

    def test_transient_authentication_errors_retain_pairing(self):
        for code in ("agent_signature_invalid", "agent_timestamp_invalid", "agent_replay_detected"):
            error = self.http_error(401, {"code": code})
            self.assertFalse(service.should_clear_device_credentials(error))

    def test_default_state_path_is_absolute(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WECHAT_COLLECTOR_STATE_FILE", None)
            path = service.state_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "wechat-collector.json")


class HistoricalDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [
            {"local_id": 1, "sort_seq": 1, "create_time": "2024-01-01T00:00:00Z"},
            {"local_id": 2, "sort_seq": 2, "create_time": "2024-01-03T00:00:00Z"},
        ]


class IncrementalFallbackDB:
    def get_new_messages(self, _chat_id, since_seq=0, limit=200):
        raise RuntimeError("database disk image is malformed")

    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [
            {"local_id": 9, "sort_seq": 9, "type": "文本", "content": "newer", "create_time": 1_700_000_009},
            {"local_id": 8, "sort_seq": 8, "type": "文本", "content": "new", "create_time": 1_700_000_008},
            {"local_id": 3, "sort_seq": 3, "type": "文本", "content": "old", "create_time": 1_700_000_003},
        ]


class TextDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [{"local_id": 8, "sort_seq": 8, "type": "文本", "content": "hello", "create_time": 1_700_000_008}]


class MediaReplayDB:
    def get_messages(self, _chat_id, limit=1000, offset=0):
        return [
            {
                "local_id": 1,
                "sort_seq": 1,
                "type": 3,
                "content": "old image",
                "create_time": "2026-09-26T04:58:00Z",
            },
            {
                "local_id": 2,
                "sort_seq": 2,
                "type": 3,
                "content": "new image",
                "create_time": "2026-09-27T07:14:00Z",
            },
        ]


class ContactDB:
    def __init__(self):
        self._db_files = [("contact.db", "contact.db", 0)]
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE contact ("
            "id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT, alias TEXT"
            ")"
        )
        self.connection.executemany(
            "INSERT INTO contact (id, username, nick_name, remark, alias) VALUES (?, ?, ?, ?, ?)",
            [
                (1, "notifymessage", "服务通知", "", ""),
                (2, "wxid_selected", "KO", "杨静涵", ""),
                (3, "wxid_symbols", "......", "", ""),
            ],
        )
        self.connection.commit()

    def _open(self, _relative_path):
        return self.connection


class CollectorServiceTest(unittest.TestCase):
    def setUp(self):
        self.original = {name: getattr(service, name) for name in ("db", "knowledge", "download_attachment", "upload_attachment", "save_state")}
        service.binding.clear(); service.binding.update({"status": "running", "wxid": "wxid-test"})
        service.device_identity.clear(); service.device_identity.update({"device_id": "device-1", "device_key": "device-key"})
        service.config.clear(); service.config.update({"enabled": True, "listen_mode": "whitelist", "selected_conversations": ["chat"], "connector_id": "account"})
        service.checkpoints.clear(); service.replayed_media.clear(); service.db = FakeDB(); self.calls = []
        def knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("/internal/devices/device-1/collectors"):
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

    def test_parse_time_accepts_numeric_string_without_using_current_time(self):
        parsed = service.parse_time("1721000000000")
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.month, 7)

    def test_contacts_returns_all_rows_in_wechat_contact_order(self):
        original_db = service.db
        try:
            service.db = ContactDB()
            result = service.contacts("", "local-development-only")
            self.assertEqual(result["total"], 3)
            self.assertEqual(
                [(item["username"], item["nick_name"], item["remark"]) for item in result["contacts"]],
                [
                    ("notifymessage", "服务通知", ""),
                    ("wxid_selected", "KO", "杨静涵"),
                    ("wxid_symbols", "......", ""),
                ],
            )
        finally:
            service.db = original_db

    def test_contact_book_sync_pushes_owner_local_rows(self):
        original_db = service.db
        try:
            service.db = ContactDB()
            service.sync_contact_book()
        finally:
            service.db = original_db
        payload = next(
            call[2] for call in self.calls if call[0].endswith("/internal/wechat/contacts")
        )
        self.assertEqual(payload["connector_id"], "account")
        self.assertTrue(payload["complete"])
        self.assertEqual(
            [
                (item["external_user_id"], item["nick_name"], item["remark"])
                for item in payload["items"]
            ],
            [
                ("notifymessage", "服务通知", ""),
                ("wxid_selected", "KO", "杨静涵"),
                ("wxid_symbols", "......", ""),
            ],
        )

    def test_contact_book_sync_skips_when_contact_table_is_unavailable(self):
        original_db = service.db
        try:
            service.db = FakeDB()
            service.sync_contact_book()
        finally:
            service.db = original_db
        self.assertFalse(
            any(call[0].endswith("/internal/wechat/contacts") for call in self.calls)
        )

    def test_heartbeat_stop_command_updates_local_state_and_acknowledges(self):
        service.apply_heartbeat_response(
            {
                "desired_state": {
                    "desired_status": "stopped",
                    "enabled": True,
                    "selected_conversations": ["chat"],
                    "listen_mode": "whitelist",
                    "config_version": 3,
                },
                "commands": [
                    {
                        "command_id": "command-1",
                        "command_type": "wechat.collector.stop",
                        "payload": {
                            "desired_status": "stopped",
                            "enabled": True,
                            "selected_conversations": ["chat"],
                            "listen_mode": "whitelist",
                            "config_version": 3,
                        },
                    }
                ],
            }
        )
        self.assertEqual(service.binding["status"], "stopped")
        self.assertEqual(service.config["config_version"], 3)
        self.assertTrue(
            any(
                path.endswith("/commands/command-1/ack")
                and payload.get("status") == "acknowledged"
                for path, method, payload in self.calls
                if method == "POST"
            )
        )

    def test_contacts_snapshot_uses_device_uplink(self):
        original_db = service.db
        try:
            service.db = ContactDB()
            result = service.upload_wechat_snapshot(
                "contacts", service.local_contact_items()
            )
        finally:
            service.db = original_db
        self.assertEqual(result, {})
        snapshot = next(
            payload
            for path, method, payload in self.calls
            if path.endswith("/wechat/snapshot") and method == "POST"
        )
        self.assertEqual(snapshot["snapshot_type"], "contacts")
        self.assertEqual(len(snapshot["items"]), 3)
        self.assertEqual(snapshot["items"][0]["username"], "notifymessage")

    def test_local_account_scan_uses_configured_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / "wxid-local_abcd"
            (account / "db_storage").mkdir(parents=True)
            original = os.environ.get("WECHAT_DATA_ROOTS")
            os.environ["WECHAT_DATA_ROOTS"] = directory
            try:
                items = service.scan_local_accounts()
            finally:
                if original is None:
                    os.environ.pop("WECHAT_DATA_ROOTS", None)
                else:
                    os.environ["WECHAT_DATA_ROOTS"] = original
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["wxid"], "wxid-local_abcd")
            self.assertEqual(Path(items[0]["db_dir"]), account.resolve())

    def test_pairing_keeps_database_path_local_and_uses_device_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / "wxid-local"
            (account / "db_storage").mkdir(parents=True)
            original = os.environ.get("WECHAT_DATA_ROOTS")
            os.environ["WECHAT_DATA_ROOTS"] = directory
            calls = []

            def pair_knowledge(path, method="GET", payload=None):
                calls.append((path, method, payload))
                if path.endswith("/internal/wechat/pair"):
                    return {
                        "device_id": "device-paired",
                        "device_key": "device-secret",
                        "connector_id": "connector-paired",
                        "platform": "wechat",
                    }
                if "/internal/wechat/bootstrap" in path:
                    return {
                        "status": "bound",
                        "connector": {
                            "id": "connector-paired",
                            "external_account_id": "wxid-local",
                            "status": "active",
                        },
                        "config": {},
                        "runtime": {"status": "running"},
                        "assignments": [],
                    }
                return {}

            service.knowledge = pair_knowledge
            try:
                result = service.pair_local_device(
                    "pairing-1", "code-1", "wxid-local", "agent"
                )
            finally:
                if original is None:
                    os.environ.pop("WECHAT_DATA_ROOTS", None)
                else:
                    os.environ["WECHAT_DATA_ROOTS"] = original

            self.assertEqual(result["device"]["device_id"], "device-paired")
            self.assertEqual(service.binding["db_dir"], str(account.resolve()))
            pair_call = next(call for call in calls if call[0].endswith("/internal/wechat/pair"))
            self.assertNotIn("db_dir", pair_call[2])
            self.assertIn("path_fingerprint", pair_call[2])

    def test_device_requests_are_hmac_signed(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def open_request(request, timeout=30):
            captured["request"] = request
            return Response()

        service.device_identity.update({"device_id": "device-1", "device_key": "device-secret"})
        with mock.patch("urllib.request.urlopen", open_request):
            self.original["knowledge"]("/api/knowledge/v1/internal/wechat/bootstrap?device_id=device-1")
        headers = captured["request"].headers
        self.assertEqual(headers.get("X-agent-device-key"), "device-secret")
        self.assertTrue(headers.get("X-agent-signature"))
        self.assertNotIn("X-service-token", headers)

    def tearDown(self):
        for name, value in self.original.items(): setattr(service, name, value)

    def test_media_metadata_supports_image_file_and_video(self):
        self.assertEqual(service.normalized_type({"type": 3}), "image")
        self.assertEqual(service.normalized_type({"type": 49}), "text")
        self.assertEqual(service.normalized_type({"type": 43}), "video")
        self.assertEqual(service.normalized_type({"type": "视频"}), "video")
        self.assertEqual(service.media_type({"type": 43}), "video")
        self.assertEqual(service.media_type({"type": "视频"}), "video")
        self.assertEqual(service.attachment_metadata("chat", {"type": 43, "local_id": 9})[0]["file_name"], "video.mp4")

    def test_incremental_read_falls_back_when_wechat_shard_is_rewritten(self):
        rows = service.messages_after(IncrementalFallbackDB(), "chat", since=3, limit=10)
        self.assertEqual([row["sort_seq"] for row in rows], [8, 9])

    def test_initial_history_page_respects_requested_start_time(self):
        rows = service.messages_after(
            HistoricalDB(),
            "chat",
            since=0,
            start_at="2024-01-02T00:00:00Z",
        )
        self.assertEqual([row["sort_seq"] for row in rows], [2])

    def test_media_replay_respects_requested_start_time(self):
        rows = service.media_replay_candidates(
            MediaReplayDB(),
            "chat",
            start_at="2026-09-27T07:13:00Z",
            seen_ids=set(),
            already_replayed=set(),
            limit=10,
        )

        self.assertEqual([row["sort_seq"] for row in rows], [2])

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

    def test_private_sender_falls_back_to_conversation_nickname(self):
        original = dict(service.binding)
        try:
            service.binding["wxid"] = "wxid_me"
            sender, display = service.resolve_sender(
                {"sender_username": "wxid_other", "content": "hello"},
                {},
                conversation_type="private",
                conversation_name="霜序十四寒",
            )
            self.assertEqual((sender, display), ("wxid_other", "霜序十四寒"))
            sender, display = service.resolve_sender(
                {"sender_username": "wxid_other", "content": "hello"},
                {},
                conversation_type="group",
                conversation_name="数据252",
            )
            self.assertEqual((sender, display), ("wxid_other", "wxid_other"))
        finally:
            service.binding.clear()
            service.binding.update(original)

    def test_private_sender_uses_conversation_identity_when_provider_sender_is_stale(self):
        original = dict(service.binding)
        try:
            service.binding["wxid"] = "wxid_me_account"
            sender, display = service.resolve_sender(
                {"sender_username": "wxid_stale", "content": "没看手机"},
                {"wxid_stale": "错误联系人"},
                conversation_type="private",
                conversation_id="wxid_partner",
                conversation_name="小呆呆仓鼠",
            )
            self.assertEqual((sender, display), ("wxid_partner", "小呆呆仓鼠"))
        finally:
            service.binding.clear()
            service.binding.update(original)

    def test_private_sender_keeps_self_identity_with_desktop_account_suffix(self):
        original = dict(service.binding)
        try:
            service.binding["wxid"] = "wxid_me_account_46ff"
            sender, display = service.resolve_sender(
                {"sender_username": "wxid_me_account", "content": "我在"},
                {"wxid_me_account": "稻成"},
                conversation_type="private",
                conversation_id="wxid_partner",
                conversation_name="对方",
            )
            self.assertEqual((sender, display), ("wxid_me_account", "稻成"))
        finally:
            service.binding.clear()
            service.binding.update(original)

    def test_media_xml_and_attachment_metadata_are_not_message_text(self):
        xml = '<msg><appmsg><type>6</type><title>安排.docx</title></appmsg></msg>'
        attachment = [{"file_name": "安排.docx"}]
        self.assertEqual(service.message_content({"content": xml}, attachment), "")
        self.assertEqual(service.message_content({"content": '{"file_key":"k","file_name":"安排.docx"}'}, attachment), "")
        self.assertEqual(service.message_content({"content": "请查收"}, attachment), "请查收")

    def test_message_external_id_prefers_wechat_server_id(self):
        identity = service.message_external_id(
            "chat@chatroom",
            {"server_id": 987654, "local_id": 11, "create_time": 1_700_000_000},
            "wxid_sender",
            "content-hash",
            [],
        )
        self.assertEqual(identity, "wechat:chat@chatroom:server:987654")

    def test_collect_once_uses_stable_message_and_attachment_ids(self):
        class ServerIDDB:
            def get_messages(self, _chat_id, limit=1000, offset=0):
                return [{
                    "local_id": 11,
                    "server_id": 987654,
                    "sort_seq": 11,
                    "type": 3,
                    "content": "image",
                    "create_time": 1_700_000_000,
                }]

        service.db = ServerIDDB()
        service.collect_once()
        payload = next(call[2] for call in self.calls if call[0].endswith("/collector/messages"))
        self.assertEqual(payload["external_message_id"], "wechat:chat:server:987654")
        self.assertEqual(
            payload["attachments"][0]["external_attachment_id"],
            "wechat:chat:server:987654:attachment:0",
        )

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

    def test_conflicting_message_does_not_starve_later_conversations(self):
        service.db = TextDB()
        service.config["selected_conversations"] = ["chat-a", "chat-b"]

        def conflict_then_success(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("/internal/devices/device-1/collectors"):
                return {"items": [
                    {"collector": {"id": "collector-a", "status": "active"}, "conversation": {"status": "active", "external_conversation_id": "chat-a"}},
                    {"collector": {"id": "collector-b", "status": "active"}, "conversation": {"status": "active", "external_conversation_id": "chat-b"}},
                ]}
            if path.endswith("/collector-a/messages"):
                raise urllib.error.HTTPError(path, 409, "Conflict", {}, io.BytesIO(b'{"code":"external_id_conflict"}'))
            if path.endswith("/messages"):
                return {"attachments": []}
            return {}

        service.knowledge = conflict_then_success
        service.collect_once()
        self.assertTrue(any(call[0].endswith("/collector-b/messages") for call in self.calls))
        self.assertTrue(any(call[0].endswith("/collector-b/heartbeat") for call in self.calls))
        self.assertEqual(service.checkpoints["collector-a"], 8)
        self.assertEqual(service.checkpoints["collector-b"], 8)

    def test_paused_conversation_is_not_collected(self):
        self.calls.clear()
        original_knowledge = service.knowledge

        def paused_knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("/internal/devices/device-1/collectors"):
                return {"items": [{"collector": {"id": "collector", "status": "active"}, "conversation": {"status": "paused", "external_conversation_id": "chat"}}]}
            return {}

        service.knowledge = paused_knowledge
        try:
            service.collect_once()
        finally:
            service.knowledge = original_knowledge
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in self.calls], ["collectors"])

    def test_system_paused_conversation_is_recovered_by_heartbeat(self):
        self.calls.clear()

        def system_paused_knowledge(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            if path.endswith("/internal/devices/device-1/collectors"):
                return {"items": [{
                    "collector": {"id": "collector", "status": "active"},
                    "conversation": {
                        "status": "paused",
                        "pause_reason": "no_available_collector",
                        "external_conversation_id": "chat",
                    },
                }]}
            if path.endswith("/messages"):
                return {"attachments": [{"id": "attachment", "content_status": "ready"}]}
            return {}

        original_knowledge = service.knowledge
        service.knowledge = system_paused_knowledge
        try:
            service.collect_once()
        finally:
            service.knowledge = original_knowledge

        paths = [call[0] for call in self.calls]
        self.assertTrue(any(path.endswith("/collector/heartbeat") for path in paths))
        self.assertTrue(any(path.endswith("/collector/messages") for path in paths))

    def test_local_state_restores_device_binding_and_checkpoint_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "device": {"device_id": "device-1", "device_key": "device-key"},
                "binding": {"wxid": "wxid-test", "db_dir": "C:/local", "status": "running"},
                "config": {"selected_conversations": ["stale-chat"]},
                "checkpoints": {"collector": 42},
                "replayed_media": {"collector": ["7", "8"]},
            }), encoding="utf-8")
            original_path = os.environ.get("WECHAT_COLLECTOR_STATE_FILE")
            os.environ["WECHAT_COLLECTOR_STATE_FILE"] = str(path)
            try:
                service.binding.clear()
                service.config.clear()
                service.checkpoints.clear()
                service.replayed_media.clear()
                service.load_state()
            finally:
                if original_path is None:
                    os.environ.pop("WECHAT_COLLECTOR_STATE_FILE", None)
                else:
                    os.environ["WECHAT_COLLECTOR_STATE_FILE"] = original_path

            self.assertEqual(service.device_identity["device_id"], "device-1")
            self.assertEqual(service.binding["db_dir"], "C:/local")
            self.assertEqual(service.config, {})
            self.assertEqual(service.checkpoints, {"collector": 42})
            self.assertEqual(service.replayed_media, {"collector": {"7", "8"}})

    def test_wechat_data_roots_uses_windows_documents_location(self):
        with tempfile.TemporaryDirectory() as directory:
            documents = Path(directory)
            original_documents_dir = service.windows_documents_dir
            original_roots = os.environ.pop("WECHAT_DATA_ROOTS", None)
            service.windows_documents_dir = lambda: documents
            try:
                roots = service.wechat_data_roots()
            finally:
                service.windows_documents_dir = original_documents_dir
                if original_roots is not None:
                    os.environ["WECHAT_DATA_ROOTS"] = original_roots

            self.assertEqual(roots[0], documents / "xwechat_files")
            self.assertEqual(roots[1], documents / "WeChat Files")

    def test_manual_account_path_accepts_desktop_account_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_dir = root / "wxid_manual_ab12"
            (account_dir / "db_storage").mkdir(parents=True)

            account = service.local_account_from_db_dir(str(root), "wxid_manual")

            self.assertEqual(account["wxid"], "wxid_manual_ab12")
            self.assertEqual(Path(account["db_dir"]), account_dir.resolve())
            self.assertEqual(Path(account["root"]), root.resolve())

    def test_manual_browser_pair_uses_explicit_account_path(self):
        with tempfile.TemporaryDirectory() as directory:
            account_dir = Path(directory) / "wxid_manual_ab12"
            (account_dir / "db_storage").mkdir(parents=True)
            original_validate = service.validate_browser_pairing
            original_pair = service.pair_local_device
            captured = {}

            def validate(_pairing_id, _pairing_code):
                return None

            def pair(pairing_id, pairing_code, wxid, agent_version, account=None):
                captured.update({
                    "pairing_id": pairing_id,
                    "pairing_code": pairing_code,
                    "wxid": wxid,
                    "agent_version": agent_version,
                    "account": account,
                })
                return {"device": {"device_id": "device-1"}, "binding": {"connector_id": "connector-1"}}

            service.validate_browser_pairing = validate
            service.pair_local_device = pair
            try:
                result = service.browser_pair(service.BrowserPairRequest(
                    pairing_id="pairing-1",
                    pairing_code="code-1",
                    wxid="wxid_manual",
                    db_dir=str(account_dir),
                ))
            finally:
                service.validate_browser_pairing = original_validate
                service.pair_local_device = original_pair

            self.assertEqual(result["status"], "paired")
            self.assertEqual(result["connector_id"], "connector-1")
            self.assertEqual(captured["wxid"], "wxid_manual_ab12")
            self.assertEqual(captured["account"]["db_dir"], str(account_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
