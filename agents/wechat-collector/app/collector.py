from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from wechatauto import MediaDownloader, WeChatDB
except ImportError as exc:  # pragma: no cover - exercised on machines without the optional reader
    MediaDownloader = None
    WeChatDB = None
    _WECHAT_IMPORT_ERROR = exc
else:
    _WECHAT_IMPORT_ERROR = None

from .config import Settings
from .knowledge_client import KnowledgeClient, KnowledgeError
from .security import LocalDatabaseError, content_hash, payload_hash, validate_database_path

LOGGER = logging.getLogger(__name__)


def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000: timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)
    text = str(value or "").strip()
    if text.isdigit(): return parse_time(int(text))
    if text:
        try: return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError: pass
    return datetime.now(timezone.utc)


def normalized_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("message_type") or raw.get("local_type") or "text").lower()
    if value in {"49", "file", "文件"}: return "file"
    if value in {"3", "image", "图片"}: return "image"
    if value in {"10000", "system", "系统消息"}: return "system"
    return "text"


def display_name(db: Any, chat_id: str, session: dict[str, Any]) -> str:
    if chat_id.lower().endswith("@chatroom"):
        return str(session.get("display_name") or session.get("name") or chat_id)
    try:
        return str(db.get_nickname(chat_id) or session.get("display_name") or chat_id)
    except Exception:
        return str(session.get("display_name") or chat_id)


def safe_file_name(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", "", Path(str(value or "attachment").replace("\\", "/")).name).strip()
    return value[:255] or "attachment"


class WeChatAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        if WeChatDB is None:
            raise RuntimeError(f"wechatauto is required for the WeChat collector: {_WECHAT_IMPORT_ERROR}")
        try:
            root, account, fingerprint = validate_database_path(settings.wechat_database_dir, settings.wechat_id)
        except LocalDatabaseError:
            self._report_pairing_failure(settings)
            raise
        self.root, self.account, self.path_fingerprint = root, account, fingerprint
        self.db = WeChatDB(account=account, db_dir=str(root))
        # Force a real database read during startup. A configured path that
        # cannot be decoded must fail explicitly instead of reporting success.
        self.db.get_sessions(limit=1)
        self.client = KnowledgeClient(settings.knowledge_base_url)
        self.state_path = Path(settings.state_file).expanduser()
        self.state = self._load_state()
        self.checkpoints: dict[str, int] = {str(k): int(v) for k, v in self.state.get("checkpoints", {}).items()}
        self.last_discovery = 0.0
        self.media = MediaDownloader(self.db) if MediaDownloader is not None else None

    @staticmethod
    def _report_pairing_failure(settings: Settings) -> None:
        if not settings.pairing_id or not settings.pairing_code:
            return
        try:
            KnowledgeClient(settings.knowledge_base_url).fail_pairing(settings.pairing_id, settings.pairing_code, "wechat_path_invalid")
        except Exception:
            # The local validation result is still authoritative for the Agent;
            # a temporary Knowledge outage must not turn into a retry storm.
            LOGGER.warning("could not report WeChat pairing failure")

    def _load_state(self) -> dict[str, Any]:
        try:
            value = __import__("json").loads(self.state_path.read_text(encoding="utf-8"))
            if value.get("wxid") not in {None, "", self.settings.wechat_id}: raise LocalDatabaseError("stored agent state belongs to a different wxid")
            stored_fingerprint = str(value.get("path_fingerprint") or "")
            if stored_fingerprint and stored_fingerprint != self.path_fingerprint: raise LocalDatabaseError("stored agent state belongs to a different database")
            return value
        except FileNotFoundError: return {}
        except Exception as exc: raise LocalDatabaseError(f"cannot read agent state: {exc}") from exc

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"device_id": self.client.device_id, "device_key": self.client.device_key, "wxid": self.settings.wechat_id, "path_fingerprint": self.path_fingerprint, "agent_version": self.settings.agent_version, "checkpoints": self.checkpoints}
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        temp.replace(self.state_path)
        try:
            self.state_path.chmod(0o600)
        except OSError:
            pass

    def pair_if_needed(self) -> None:
        device_id = self.state.get("device_id") or self.settings.device_id
        device_key = self.settings.device_key or str(self.state.get("device_key") or "")
        if not device_key and self.settings.pairing_id and self.settings.pairing_code:
            try:
                response = self.client.pair(self.settings.pairing_id, self.settings.pairing_code, self.settings.wechat_id, self.path_fingerprint, self.settings.agent_version)
            except KnowledgeError as exc:
                if exc.code == "wechat_pairing_expired":
                    raise RuntimeError("wechat pairing has expired; create a new pairing code") from exc
                raise RuntimeError("wechat pairing failed; verify the pairing id, code, and WeChat ID") from exc
            device_id = response.get("device_id", "")
            device_key = response.get("device_key", "")
            if not device_id or not device_key: raise RuntimeError("knowledge pairing response did not contain a device credential")
            self.client.set_device(device_id, device_key)
            self.state["device_id"] = device_id
            self.state["device_key"] = device_key
            self.state["wxid"] = self.settings.wechat_id
            self._save_state()
        if not device_id or not device_key:
            raise RuntimeError("agent is not paired; provide PAIRING_ID and PAIRING_CODE or a local device state")
        self.client.set_device(device_id, device_key)

    def discover(self) -> None:
        items = []
        for session in self.db.get_sessions(limit=1000):
            chat_id = str(session.get("username") or session.get("chat_id") or "").strip()
            if not chat_id: continue
            kind = "group" if chat_id.endswith("@chatroom") else "private"
            items.append({"external_id": chat_id, "name": display_name(self.db, chat_id, session), "conversation_type": kind, "member_count": 0 if kind == "group" else 2, "metadata": {"path_fingerprint": self.path_fingerprint}})
        self.client.discovery(items)
        self.last_discovery = time.monotonic()

    def _attachment_metadata(self, chat_id: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
        if normalized_type(raw) not in {"file", "image"}: return []
        local_id = str(raw.get("local_id") or "0")
        content = str(raw.get("content") or "")
        match = re.search(r"<title>\s*([^<]+?)\s*</title>", content, re.I | re.S)
        name = safe_file_name(match.group(1) if match else ("image.bin" if normalized_type(raw) == "image" else "attachment.bin"))
        mime = mimetypes.guess_type(name)[0] or ("image/jpeg" if normalized_type(raw) == "image" else "application/octet-stream")
        return [{"external_attachment_id": f"{self.settings.wechat_id}:{chat_id}:{local_id}:0", "file_name": name, "mime_type": mime, "size_bytes": 0, "content_hash": ""}]

    def _message(self, collector: dict[str, Any], conversation: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        chat_id = conversation.get("external_conversation_id", "")
        local_id = str(raw.get("local_id") or raw.get("server_id") or raw.get("sort_seq") or "0")
        external_id = f"{self.settings.wechat_id}:{chat_id}:{local_id}"
        content = str(raw.get("content") or "")
        sender = str(raw.get("sender_username") or raw.get("sender_id") or self.settings.wechat_id)
        sent_at = parse_time(raw.get("create_time"))
        sent_at_text = sent_at.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
        value = {"collector_id": collector["id"], "external_conversation_id": chat_id, "external_message_id": external_id, "sender_external_id": sender, "sender_display_name": sender, "message_type": normalized_type(raw), "content": content, "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(), "sent_at": sent_at_text, "cursor": str(raw.get("sort_seq") or raw.get("local_id") or ""), "attachments": self._attachment_metadata(chat_id, raw)}
        value["payload_hash"] = payload_hash(value)
        return value

    def _download_attachment(self, chat_id: str, raw: dict[str, Any], attachment: dict[str, Any]) -> tuple[Path, Path] | None:
        if not self.media: return None
        try: local_id = int(raw.get("local_id"))
        except (TypeError, ValueError): return None
        target = Path(tempfile.mkdtemp(prefix="wechat-attachment-"))
        keep_for_upload = False
        try:
            method = "download_image" if normalized_type(raw) == "image" else "download_file"
            result = getattr(self.media, method)(chat_id, local_id, str(target))
            if not result or not Path(result).is_file(): return None
            path = Path(result).resolve()
            resolved_target = target.resolve()
            if resolved_target not in path.parents:
                return None
            if path.stat().st_size > self.settings.max_attachment_bytes: return None
            keep_for_upload = True
            return path, resolved_target
        except Exception:
            return None
        finally:
            if not keep_for_upload:
                shutil.rmtree(target, ignore_errors=True)

    def poll(self, assignments: list[dict]) -> None:
        first_error: Exception | None = None
        for item in assignments:
            collector = item.get("collector") or {}
            conversation = item.get("conversation") or {}
            if collector.get("status") != "active": continue
            collector_id = str(collector.get("id") or "")
            next_poll_at = collector.get("next_poll_at")
            if next_poll_at and parse_time(next_poll_at) > datetime.now(timezone.utc):
                continue
            try:
                self._poll_collector(collector, conversation)
            except KnowledgeError as exc:
                if exc.code in {"agent_device_expired", "agent_device_invalid", "agent_signature_invalid", "unauthorized"}:
                    raise
                self._report_collector_failure(collector_id, exc.code)
                LOGGER.error("collector cycle failed: %s", exc.code)
                if first_error is None:
                    first_error = exc
            except Exception as exc:
                self._report_collector_failure(collector_id, "collector_poll_failed")
                LOGGER.error("collector cycle failed")
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _poll_collector(self, collector: dict, conversation: dict) -> None:
        trace_scope = getattr(self.client, "trace_scope", None)
        if callable(trace_scope):
            with trace_scope():
                self._poll_collector_once(collector, conversation)
            return
        self._poll_collector_once(collector, conversation)

    def _poll_collector_once(self, collector: dict, conversation: dict) -> None:
        collector_id = str(collector.get("id") or "")
        self.client.heartbeat(collector_id, self.settings.agent_version)
        chat_id = str(conversation.get("external_conversation_id") or "")
        if not chat_id: return
        since = self.checkpoints.get(collector_id, 0)
        if since:
            rows = self.db.get_new_messages(chat_id, since_seq=since, limit=200)
        else:
            rows = list(reversed(self.db.get_messages(chat_id, limit=1000, offset=0)))
            start = conversation.get("effective_start_at") or conversation.get("requested_start_at")
            if start:
                bound = parse_time(start)
                rows = [row for row in rows if parse_time(row.get("create_time")) >= bound]
        for raw in rows:
            value = self._message(collector, conversation, raw)
            response = self.client.message(collector_id, value)
            expected_attachments = value.get("attachments", [])
            saved_attachments = response.get("attachments", [])
            if len(saved_attachments) != len(expected_attachments):
                raise RuntimeError("knowledge service returned incomplete attachment metadata")
            for attachment, metadata in zip(expected_attachments, saved_attachments):
                # A previous attempt may have uploaded the content but failed
                # while committing the cursor. Do not download and upload the
                # same external attachment again when the server already has a
                # ready object.
                if str(metadata.get("content_status") or "") == "ready":
                    continue
                downloaded = self._download_attachment(chat_id, raw, attachment)
                if not downloaded:
                    raise RuntimeError(f"attachment content is unavailable: {attachment.get('external_attachment_id', '')}")
                path, temp_root = downloaded
                try:
                    with path.open("rb") as stream:
                        digest, size = content_hash(stream)
                    attachment["content_hash"] = digest
                    self.client.upload_attachment(collector_id, metadata.get("id", attachment.get("external_attachment_id", "")), path, attachment["file_name"], attachment["mime_type"], digest)
                finally:
                    shutil.rmtree(temp_root, ignore_errors=True)
            seq = int(raw.get("sort_seq") or raw.get("local_id") or since)
            # The server cursor is committed only after this message and
            # every attachment upload have succeeded. A failed commit is
            # intentionally retried from the previous local checkpoint.
            self.client.advance_cursor(collector_id, str(value.get("cursor") or seq))
            self.checkpoints[collector_id] = max(self.checkpoints.get(collector_id, 0), seq)
            self._save_state()

    def _report_collector_failure(self, collector_id: str, failure_code: str) -> None:
        reporter = getattr(self.client, "failure", None)
        if not callable(reporter) or not collector_id:
            return
        try:
            reporter(collector_id, failure_code)
        except Exception:
            # Failure telemetry must never hide the original collection error.
            LOGGER.warning("could not report collector failure")

    def run_once(self) -> None:
        if time.monotonic() - self.last_discovery >= self.settings.discovery_interval: self.discover()
        self.poll(self.client.collectors())

    def run_forever(self) -> None:
        self.pair_if_needed()
        self.discover()
        while True:
            try: self.run_once()
            except KnowledgeError as exc:
                if exc.code in {"agent_device_expired", "agent_device_invalid", "unauthorized"}:
                    LOGGER.error("collector authorization is no longer valid; re-pair the agent")
                    raise
                LOGGER.error("collector cycle failed: %s", exc.code)
            except Exception:
                LOGGER.error("collector cycle failed")
            time.sleep(self.settings.poll_interval)
