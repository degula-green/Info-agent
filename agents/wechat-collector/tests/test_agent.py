import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import collector as collector_module
from app.collector import WeChatAgent
from app.config import Settings
from app.knowledge_client import KnowledgeClient, KnowledgeError
from app.security import LocalDatabaseError, validate_database_path


class FakeDB:
    def __init__(self, account, db_dir):
        self.account = account
        self.db_dir = db_dir

    def get_sessions(self, limit=1):
        return []


def settings_for(root, state_file, **overrides):
    values = dict(
        knowledge_base_url="http://127.0.0.1:8090",
        pairing_id="",
        pairing_code="",
        wechat_id="wxid_a",
        wechat_database_dir=str(root),
        device_id="device",
        device_key="device-key",
        state_file=str(state_file),
        agent_version="test-agent",
        poll_interval=1.0,
        discovery_interval=30.0,
        max_attachment_bytes=1024,
    )
    values.update(overrides)
    return Settings(**values)


class AgentTests(unittest.TestCase):
    def _poll_agent(self, root, client, db, media):
        agent = object.__new__(WeChatAgent)
        agent.settings = settings_for(root, root / "state.json")
        agent.client = client
        agent.db = db
        agent.media = media
        agent.checkpoints = {}
        agent.state = {}
        agent.last_discovery = 0.0
        agent._save_state = Mock()
        return agent

    def test_state_recovery_rejects_different_database(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            account = root / "wxid_a_1234"
            (account / "db_storage").mkdir(parents=True)
            _, _, fingerprint = validate_database_path(str(root), "wxid_a")
            state_file = root / "state.json"
            state_file.write_text(json.dumps({"wxid": "wxid_a", "path_fingerprint": "0" * 64}), encoding="utf-8")
            with patch.object(collector_module, "WeChatDB", FakeDB):
                with self.assertRaises(Exception):
                    WeChatAgent(settings_for(root, state_file))
            state_file.write_text(json.dumps({"wxid": "wxid_a", "path_fingerprint": fingerprint}), encoding="utf-8")
            with patch.object(collector_module, "WeChatDB", FakeDB):
                agent = WeChatAgent(settings_for(root, state_file))
            self.assertEqual(agent.path_fingerprint, fingerprint)

    def test_invalid_database_path_reports_safe_pairing_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = settings_for(root, root / "state.json", pairing_id="pair-1", pairing_code="code-1", wechat_database_dir=str(root / "missing"), device_id="", device_key="")
            client = Mock()
            with patch.object(collector_module, "WeChatDB", FakeDB), patch.object(collector_module, "KnowledgeClient", return_value=client):
                with self.assertRaises(LocalDatabaseError):
                    WeChatAgent(settings)
            client.fail_pairing.assert_called_once_with("pair-1", "code-1", "wechat_path_invalid")

    def test_pairing_failure_is_reported_without_leaking_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "wxid_a_1234" / "db_storage").mkdir(parents=True)

            class FailingClient:
                def __init__(self, _base_url):
                    self.device_id = ""
                    self.device_key = ""

                def pair(self, *args):
                    raise KnowledgeError("expired", "wechat_pairing_expired", 400)

            with patch.object(collector_module, "WeChatDB", FakeDB), patch.object(collector_module, "KnowledgeClient", FailingClient):
                agent = WeChatAgent(settings_for(root, Path(raw) / "state.json", pairing_id="p", pairing_code="c", device_id="", device_key=""))
                with self.assertRaisesRegex(RuntimeError, "expired"):
                    agent.pair_if_needed()

    def test_client_streams_multipart_attachment(self):
        sent = []
        connections = []

        class FakeResponse:
            status = 200

            def read(self):
                return b'{"attachment":{"id":"a1"}}'

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.headers = {}
                connections.append(self)

            def putrequest(self, method, path):
                self.method, self.path = method, path

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self):
                pass

            def send(self, value):
                sent.append(value)

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as raw:
            file = Path(raw) / "upload.bin"
            file.write_bytes(b"attachment-content")
            client = KnowledgeClient("http://localhost:8090", "device", "key")
            with patch("app.knowledge_client.http.client.HTTPConnection", FakeConnection):
                response = client.upload_attachment("collector", "a1", file, "note.txt", "text/plain", "")
            self.assertEqual(response["attachment"]["id"], "a1")
        body = b"".join(sent)
        self.assertIn(b"filename=\"note.txt\"", body)
        self.assertIn(b"attachment-content", body)
        self.assertTrue(connections[0].headers.get("X-Request-ID"))
        self.assertTrue(connections[0].headers.get("X-Trace-ID"))

    def test_client_reuses_trace_id_for_message_cycle(self):
        connections = []

        class FakeResponse:
            status = 200

            def read(self):
                return b"{}"

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.headers = {}
                connections.append(self)

            def request(self, method, path, body=None, headers=None):
                self.method, self.path, self.headers = method, path, headers or {}

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        client = KnowledgeClient("http://localhost:8090", "device", "key")
        with patch("app.knowledge_client.http.client.HTTPConnection", FakeConnection):
            with client.trace_scope("cycle-trace"):
                client.heartbeat("collector", "test-agent")
                client.message("collector", {"collector_id": "collector"})
                client.advance_cursor("collector", "1")

        self.assertEqual([item.headers["X-Trace-ID"] for item in connections], ["cycle-trace"] * 3)

    def test_attachment_download_failure_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            class PollDB(FakeDB):
                def get_messages(self, _chat_id, limit, offset):
                    return self.get_new_messages(_chat_id, 0, limit)

                def get_new_messages(self, _chat_id, since_seq, limit):
                    self.request = (since_seq, limit)
                    return [{"local_id": 1, "sort_seq": 1, "type": "file", "content": "<title>note.txt</title>", "create_time": "2026-09-05T00:00:00Z"}]

            class PollClient:
                def __init__(self):
                    self.advanced = []

                def heartbeat(self, _collector_id, _version):
                    return {}

                def message(self, _collector_id, _value):
                    return {"attachments": [{"id": "attachment-1"}]}

                def advance_cursor(self, collector_id, cursor):
                    self.advanced.append((collector_id, cursor))

            class MissingMedia:
                def download_file(self, _chat_id, _local_id, _target):
                    return None

            client = PollClient()
            agent = self._poll_agent(root, client, PollDB("wxid_a", str(root)), MissingMedia())
            with self.assertRaisesRegex(RuntimeError, "attachment content is unavailable"):
                agent.poll([{
                    "collector": {"id": "collector-1", "status": "active"},
                    "conversation": {"external_conversation_id": "chat-1"},
                }])
            self.assertEqual(client.advanced, [])
            self.assertEqual(agent.checkpoints, {})
            agent._save_state.assert_not_called()

    def test_cursor_commit_failure_does_not_write_local_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            class PollDB(FakeDB):
                def get_messages(self, _chat_id, limit, offset):
                    return self.get_new_messages(_chat_id, 0, limit)

                def get_new_messages(self, _chat_id, since_seq, limit):
                    return [{"local_id": 2, "sort_seq": 2, "type": "text", "content": "hello", "create_time": "2026-09-05T00:00:00Z"}]

            class FailingClient:
                def heartbeat(self, _collector_id, _version):
                    return {}

                def message(self, _collector_id, _value):
                    return {"attachments": []}

                def advance_cursor(self, _collector_id, _cursor):
                    raise KnowledgeError("cursor unavailable", "cursor_commit_failed", 503)

            agent = self._poll_agent(root, FailingClient(), PollDB("wxid_a", str(root)), None)
            with self.assertRaises(KnowledgeError) as raised:
                agent.poll([{
                    "collector": {"id": "collector-1", "status": "active"},
                    "conversation": {"external_conversation_id": "chat-1"},
                }])
            self.assertEqual(raised.exception.code, "cursor_commit_failed")
            self.assertEqual(agent.checkpoints, {})
            agent._save_state.assert_not_called()

    def test_ready_attachment_is_not_downloaded_again_on_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            class PollDB(FakeDB):
                def get_messages(self, _chat_id, limit, offset):
                    return self.get_new_messages(_chat_id, 0, limit)

                def get_new_messages(self, _chat_id, since_seq, limit):
                    return [{"local_id": 3, "sort_seq": 3, "type": "file", "content": "<title>note.txt</title>", "create_time": "2026-09-05T00:00:00Z"}]

            class ReadyClient:
                def __init__(self):
                    self.advanced = []

                def heartbeat(self, _collector_id, _version):
                    return {}

                def message(self, _collector_id, _value):
                    return {"attachments": [{"id": "attachment-1", "content_status": "ready"}]}

                def advance_cursor(self, collector_id, cursor):
                    self.advanced.append((collector_id, cursor))

            class UnexpectedMedia:
                def download_file(self, *_args):
                    raise AssertionError("ready attachment should not be downloaded")

            client = ReadyClient()
            agent = self._poll_agent(root, client, PollDB("wxid_a", str(root)), UnexpectedMedia())
            agent.poll([{
                "collector": {"id": "collector-1", "status": "active"},
                "conversation": {"external_conversation_id": "chat-1"},
            }])
            self.assertEqual(client.advanced, [("collector-1", "3")])
            self.assertEqual(agent.checkpoints, {"collector-1": 3})

    def test_successful_attachment_upload_removes_temporary_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            download_root = root / "wechat-download"

            class PollDB(FakeDB):
                def get_messages(self, _chat_id, limit, offset):
                    return [{"local_id": 4, "sort_seq": 4, "type": "file", "content": "<title>note.txt</title>", "create_time": "2026-09-05T00:00:00Z"}]

            class UploadClient:
                def heartbeat(self, _collector_id, _version): return {}
                def message(self, _collector_id, _value): return {"attachments": [{"id": "attachment-1"}]}
                def upload_attachment(self, _collector_id, _attachment_id, path, _name, _mime, _digest):
                    self.uploaded = path.read_bytes()
                def advance_cursor(self, _collector_id, _cursor): pass

            class Media:
                def download_file(self, _chat_id, _local_id, target):
                    target_path = Path(target)
                    target_path.mkdir(parents=True, exist_ok=True)
                    downloaded = target_path / "note.txt"
                    downloaded.write_bytes(b"attachment")
                    return str(downloaded)

            client = UploadClient()
            agent = self._poll_agent(root, client, PollDB("wxid_a", str(root)), Media())
            with patch.object(collector_module.tempfile, "mkdtemp", return_value=str(download_root)):
                agent.poll([{"collector": {"id": "collector-1", "status": "active"}, "conversation": {"external_conversation_id": "chat-1"}}])
            self.assertEqual(client.uploaded, b"attachment")
            self.assertFalse(download_root.exists())


if __name__ == "__main__":
    unittest.main()
