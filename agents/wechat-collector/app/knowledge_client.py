from __future__ import annotations

import http.client
import json
import mimetypes
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from .security import signature


class KnowledgeError(RuntimeError):
    def __init__(self, message: str, code: str = "request_failed", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class KnowledgeClient:
    def __init__(self, base_url: str, device_id: str = "", device_key: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.device_key = device_key
        self.timeout = timeout
        self._trace_id = ""

    def set_device(self, device_id: str, device_key: str) -> None:
        self.device_id, self.device_key = device_id, device_key

    @contextmanager
    def trace_scope(self, trace_id: str | None = None) -> Iterator[str]:
        """Reuse one trace ID for all requests in a collector message cycle."""
        previous = self._trace_id
        self._trace_id = trace_id or str(uuid.uuid4())
        try:
            yield self._trace_id
        finally:
            self._trace_id = previous

    def _connection(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise KnowledgeError("KNOWLEDGE_BASE_URL must be an absolute http(s) URL")
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(parsed.netloc, timeout=self.timeout), parsed.path.rstrip("/")
        return http.client.HTTPConnection(parsed.netloc, timeout=self.timeout), parsed.path.rstrip("/")

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None, signed: bool = True) -> dict:
        connection, base_path = self._connection()
        request_path = f"{base_path}{path}" if base_path else path
        request_headers = {"Accept": "application/json", "X-Request-ID": str(uuid.uuid4()), "X-Trace-ID": self._trace_id or str(uuid.uuid4())}
        request_headers.update(headers or {})
        body_hash = request_headers.get("X-Agent-Payload-Hash") or __import__("hashlib").sha256(body or b"").hexdigest()
        if signed:
            if not self.device_id or not self.device_key:
                raise KnowledgeError("agent device is not paired")
            stamp = str(int(time.time()))
            request_headers.update({
                "X-Agent-Device-Key": self.device_key,
                "X-Agent-Timestamp": stamp,
                "X-Agent-Payload-Hash": body_hash,
                "X-Agent-Signature": signature(self.device_key, stamp, method, request_path, body_hash),
            })
        try:
            connection.request(method.upper(), request_path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise KnowledgeError("knowledge service is unavailable") from exc
        finally:
            connection.close()
        parsed = {}
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {}
        if response.status >= 400:
            raise KnowledgeError(parsed.get("message", f"knowledge request failed ({response.status})"), parsed.get("code", "request_failed"), response.status)
        return parsed

    def pair(self, pairing_id: str, pairing_code: str, wxid: str, path_fingerprint: str, agent_version: str) -> dict:
        body = json.dumps({"pairing_id": pairing_id, "pairing_code": pairing_code, "wxid": wxid, "path_fingerprint": path_fingerprint, "agent_version": agent_version}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request("POST", "/internal/wechat/pair", body, {"Content-Type": "application/json"}, signed=False)

    def fail_pairing(self, pairing_id: str, pairing_code: str, failure_code: str = "wechat_path_invalid") -> dict:
        body = json.dumps({"pairing_id": pairing_id, "pairing_code": pairing_code, "failure_code": failure_code}, separators=(",", ":")).encode("utf-8")
        return self._request("POST", "/internal/wechat/pair/failure", body, {"Content-Type": "application/json"}, signed=False)

    def collectors(self) -> list[dict]:
        return self._request("GET", f"/internal/devices/{self.device_id}/collectors").get("items", [])

    def discovery(self, items: list[dict]) -> dict:
        body = json.dumps({"conversations": items}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request("POST", f"/internal/devices/{self.device_id}/discoveries", body, {"Content-Type": "application/json"})

    def heartbeat(self, collector_id: str, agent_version: str) -> dict:
        body = json.dumps({"agent_version": agent_version}, separators=(",", ":")).encode("utf-8")
        return self._request("POST", f"/internal/collectors/{collector_id}/heartbeat", body, {"Content-Type": "application/json"})

    def failure(self, collector_id: str, failure_code: str = "collector_poll_failed") -> dict:
        value = {"failure_code": str(failure_code or "collector_poll_failed")}
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return self._request("POST", f"/internal/collectors/{collector_id}/failure", body, {"Content-Type": "application/json"})

    def message(self, collector_id: str, value: dict) -> dict:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._request("POST", f"/internal/collectors/{collector_id}/messages", body, {"Content-Type": "application/json"})

    def advance_cursor(self, collector_id: str, cursor: str) -> dict:
        value = {"cursor": str(cursor or "")}
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return self._request("POST", f"/internal/collectors/{collector_id}/cursor", body, {"Content-Type": "application/json"})

    def upload_attachment(self, collector_id: str, attachment_id: str, file_path: Path, file_name: str, mime_type: str, declared_hash: str) -> dict:
        boundary = f"----knowledge-{uuid.uuid4().hex}"
        parts: list[bytes] = []
        def field(name: str, value: str) -> None:
            parts.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")])
        field("attachment_id", attachment_id)
        field("file_name", file_name)
        field("mime_type", mime_type or "application/octet-stream")
        field("content_hash", declared_hash)
        prefix = f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name.replace(chr(34), '')}\"\r\nContent-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        connection, base_path = self._connection()
        request_path = f"{base_path}/internal/collectors/{collector_id}/attachments" if base_path else f"/internal/collectors/{collector_id}/attachments"
        stamp = str(int(time.time()))
        body_hash = declared_hash
        headers = {"Accept": "application/json", "X-Request-ID": str(uuid.uuid4()), "X-Trace-ID": self._trace_id or str(uuid.uuid4()), "Content-Type": f"multipart/form-data; boundary={boundary}", "X-Agent-Device-Key": self.device_key, "X-Agent-Timestamp": stamp, "X-Agent-Payload-Hash": body_hash, "X-Agent-Signature": signature(self.device_key, stamp, "POST", request_path, body_hash), "Content-Length": str(sum(map(len, parts)) + len(prefix) + file_path.stat().st_size + len(suffix))}
        try:
            connection.putrequest("POST", request_path)
            for name, value in headers.items(): connection.putheader(name, value)
            connection.endheaders()
            for part in parts: connection.send(part)
            connection.send(prefix)
            with file_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024): connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise KnowledgeError("attachment upload failed") from exc
        finally:
            connection.close()
        try: parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError): parsed = {}
        if response.status >= 400: raise KnowledgeError(parsed.get("message", "attachment upload failed"), parsed.get("code", "attachment_upload_failed"), response.status)
        return parsed
