from __future__ import annotations
import hashlib, json, mimetypes, os, re, shutil, tempfile, threading, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from wechatauto import MediaDownloader, WeChatDB

app = FastAPI(title="info-agent-wechat-collector")
lock = threading.Lock(); binding: dict[str, Any] = {}; db: Any = None
config: dict[str, Any] = {"selected_conversations": [], "history_start_at": None, "enabled": True, "listen_mode": "whitelist", "connector_id": ""}
checkpoints: dict[str, int] = {}; worker_thread: threading.Thread | None = None
media: Any = None; bootstrap_error: str | None = None

class BindRequest(BaseModel):
    wxid: str = Field(min_length=3)
    db_dir: str = Field(min_length=3)

def auth(token: str | None) -> None:
    if token != os.getenv("COLLECTOR_INTERNAL_TOKEN", "local-development-only"): raise HTTPException(401, "invalid collector credential")

def state_path() -> Path: return Path(os.getenv("WECHAT_COLLECTOR_STATE_FILE", "./data/wechat-collector.json")).expanduser()
def load_state() -> None:
    global checkpoints
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        checkpoints = {str(k): int(v) for k, v in (value.get("checkpoints") or {}).items()}
    except (FileNotFoundError, ValueError, OSError): pass
def bootstrap_from_knowledge() -> None:
    global binding, config, bootstrap_error
    binding.clear()
    config.clear(); config.update({"selected_conversations": [], "history_start_at": None, "enabled": True, "listen_mode": "whitelist", "connector_id": ""})
    bootstrap_error = None
    try:
        connector_id = os.getenv("WECHAT_CONNECTOR_ID", "").strip()
        suffix = "?connector_id=" + connector_id if connector_id else ""
        result = knowledge("/api/knowledge/v1/internal/wechat/bootstrap" + suffix)
        entries = [result] if connector_id else (result.get("items") or [])
        runnable = [entry for entry in entries if str((entry.get("runtime") or {}).get("status") or "") != "stopped"]
        if not runnable: return
        if len(runnable) > 1:
            bootstrap_error = "multiple runnable WeChat connectors found; set WECHAT_CONNECTOR_ID"
            return
        entry = runnable[0]
        connector = entry.get("connector") or {}
        runtime = entry.get("runtime") or {}
        binding.update({"wxid": connector.get("external_account_id") or connector.get("wechat_id"), "db_dir": connector.get("database_ref"), "status": runtime.get("status") or "running", "connector_id": connector.get("id")})
        saved = entry.get("config") or {}
        config.update(saved)
        config["connector_id"] = connector.get("id")
    except Exception as exc:
        bootstrap_error = f"Knowledge bootstrap failed: {exc}"[:500]

def save_state() -> None:
    path = state_path(); path.parent.mkdir(parents=True, exist_ok=True); temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"checkpoints": checkpoints}, ensure_ascii=False), encoding="utf-8"); temp.replace(path)
def knowledge(path: str, method: str = "GET", payload: Any = None) -> dict[str, Any]:
    base = os.getenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:8090").rstrip("/"); body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, method=method, headers={"Accept": "application/json", "Content-Type": "application/json", "X-Service-Token": os.getenv("KNOWLEDGE_INTERNAL_SERVICE_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=30) as response: return json.loads(response.read().decode("utf-8") or "{}")

load_state()
bootstrap_from_knowledge()

def normalized_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("message_type") or "text").lower()
    return {"49": "file", "3": "image", "43": "file", "video": "file", "10000": "system"}.get(value, value if value in {"text", "image", "file", "mixed", "system"} else "text")

def media_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("message_type") or "").lower()
    if value in {"43", "video"}: return "video"
    return normalized_type(raw)

def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value); number /= 1000 if number > 10_000_000_000 else 1
        return datetime.fromtimestamp(number, timezone.utc)
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError): return datetime.now(timezone.utc)

def payload_hash(value: dict[str, Any]) -> str:
    raw = json.dumps({key: item for key, item in value.items() if key != "payload_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw = raw.replace("\u2028".encode("utf-8"), b"\\u2028").replace("\u2029".encode("utf-8"), b"\\u2029")
    return hashlib.sha256(raw).hexdigest()

def attachment_metadata(chat_id: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    kind = media_type(raw)
    if kind not in {"file", "image", "video"}: return []
    local_id = str(raw.get("local_id") or "0"); content = str(raw.get("content") or "")
    match = re.search(r"<title>\s*([^<]+?)\s*</title>", content, re.I | re.S)
    fallback = "image.bin" if kind == "image" else "video.mp4" if kind == "video" else "attachment.bin"
    name = Path((match.group(1) if match else fallback)).name[:255]
    mime = mimetypes.guess_type(name)[0] or ("image/jpeg" if kind == "image" else "video/mp4" if kind == "video" else "application/octet-stream")
    return [{"external_attachment_id": f"{binding.get('wxid','')}:{chat_id}:{local_id}:0", "file_name": name, "mime_type": mime, "size_bytes": 0, "content_hash": ""}]

def download_attachment(chat_id: str, raw: dict[str, Any]) -> tuple[Path, Path] | None:
    if media is None: return None
    try: local_id = int(raw.get("local_id"))
    except (TypeError, ValueError): return None
    root = Path(tempfile.mkdtemp(prefix="wechat-attachment-"))
    try:
        kind = media_type(raw)
        method = "download_image" if kind == "image" else "download_video" if kind == "video" else "download_file"
        result = getattr(media, method)(chat_id, local_id, str(root))
        if not result or not Path(result).is_file():
            shutil.rmtree(root, ignore_errors=True); return None
        path = Path(result).resolve(); resolved_root = root.resolve()
        if resolved_root not in path.parents or path.stat().st_size > int(os.getenv("WECHAT_MAX_ATTACHMENT_BYTES", str(100 * 1024 * 1024))):
            shutil.rmtree(root, ignore_errors=True); return None
        return path, resolved_root
    except Exception:
        shutil.rmtree(root, ignore_errors=True); return None

def upload_attachment(collector_id: str, attachment_id: str, path: Path, name: str, mime: str, digest: str) -> dict[str, Any]:
    boundary = "----wechat-" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    fields = [("attachment_id", attachment_id), ("file_name", name), ("mime_type", mime), ("content_hash", digest)]
    body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode() for key, value in fields)
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name.replace(chr(34), '')}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    base = os.getenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    request = urllib.request.Request(f"{base}/api/knowledge/v1/internal/collectors/{collector_id}/attachments", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Service-Token": os.getenv("KNOWLEDGE_INTERNAL_SERVICE_TOKEN", ""), "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response: return json.loads(response.read().decode() or "{}")

def collect_once() -> None:
    if binding.get("status") == "running" and config.get("enabled") and config.get("connector_id") and db is not None:
        assignments = knowledge("/api/knowledge/v1/internal/wechat/assignments?connector_id=" + str(config["connector_id"])).get("items", []); selected = set(config.get("selected_conversations") or [])
        for item in assignments:
            conversation = item.get("conversation") or {}; collector = item.get("collector") or {}; chat_id = str(conversation.get("external_conversation_id") or ""); collector_id = str(collector.get("id") or "")
            if not chat_id or not collector_id or (config.get("listen_mode") == "whitelist" and chat_id not in selected): continue
            try: since = int(str(collector.get("last_cursor") or "0"))
            except ValueError: since = checkpoints.get(collector_id, 0)
            rows = db.get_new_messages(chat_id, since_seq=since, limit=200) if since else list(reversed(db.get_messages(chat_id, limit=1000, offset=0)))
            start_at = conversation.get("effective_start_at") or conversation.get("requested_start_at") or config.get("history_start_at")
            if not since and start_at: rows = [row for row in rows if parse_time(row.get("create_time")) >= parse_time(start_at)]
            for raw in rows:
                        local_id = str(raw.get("local_id") or raw.get("server_id") or raw.get("sort_seq") or "0"); content = str(raw.get("content") or ""); cursor = str(raw.get("sort_seq") or raw.get("local_id") or "")
                        attachments = attachment_metadata(chat_id, raw)
                        value = {"collector_id": collector_id, "external_conversation_id": chat_id, "external_message_id": f"{binding.get('wxid', '')}:{chat_id}:{local_id}", "sender_external_id": str(raw.get("sender_username") or raw.get("sender_id") or binding.get("wxid", "")), "sender_display_name": str(raw.get("sender_username") or raw.get("sender_id") or binding.get("wxid", "")), "message_type": normalized_type(raw), "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(), "sent_at": parse_time(raw.get("create_time")).isoformat().replace("+00:00", "Z"), "cursor": cursor, "attachments": attachments}
                        value["payload_hash"] = payload_hash(value)
                        result = knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/messages", "POST", value)
                        saved_attachments = (result.get("attachments") or result.get("message", {}).get("attachments") or [])
                        if len(saved_attachments) != len(attachments): raise RuntimeError("knowledge returned incomplete attachment metadata")
                        for attachment, saved in zip(attachments, saved_attachments):
                            if str(saved.get("content_status") or "") == "ready": continue
                            downloaded = download_attachment(chat_id, raw)
                            if not downloaded: raise RuntimeError(f"attachment content unavailable: {attachment['external_attachment_id']}")
                            path, temp_root = downloaded
                            try:
                                digest = hashlib.sha256(path.read_bytes()).hexdigest(); attachment["content_hash"] = digest
                                upload_attachment(collector_id, str(saved.get("id") or attachment["external_attachment_id"]), path, attachment["file_name"], attachment["mime_type"], digest)
                            finally: shutil.rmtree(temp_root, ignore_errors=True)
                        knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/cursor", "POST", {"cursor": cursor})
                        try: numeric_cursor = int(cursor or 0)
                        except ValueError: numeric_cursor = checkpoints.get(collector_id, 0)
                        checkpoints[collector_id] = max(checkpoints.get(collector_id, 0), numeric_cursor); save_state()
            knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/heartbeat", "POST", {"agent_version": "server-wechat-collector"})

def collector_worker() -> None:
    while True:
        try:
            collect_once()
        except Exception as exc: binding["last_error"] = str(exc)[:500]
        time.sleep(float(os.getenv("WECHAT_COLLECTOR_POLL_INTERVAL", "5")))

def open_db(path_value: str, wxid: str) -> Any:
    path = Path(path_value).expanduser()
    if not path.is_absolute() or not path.exists(): raise ValueError("db_dir must be an existing local absolute path")
    if path.is_file():
        storage = next((p for p in path.parents if p.name == "db_storage"), None)
        if storage is None: raise ValueError("database file must be located below an account db_storage directory")
        path = storage.parent
    if (path / "db_storage").is_dir(): root, account = path.parent, path.name
    else:
        root = path; candidates = [p for p in root.iterdir() if p.is_dir() and (p / "db_storage").is_dir()]; normalized = wxid.lower(); matches = [p for p in candidates if p.name.lower().removesuffix("_" + p.name[-4:]) == normalized]; selected = matches[0] if len(matches) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if selected is None: raise ValueError("db_dir must contain one matching WeChat account directory")
        account = selected.name
    if not (root / account / "db_storage").is_dir(): raise ValueError("db_dir does not contain a readable WeChat db_storage directory")
    opened = WeChatDB(account=account, db_dir=str(root)); opened.get_sessions(limit=1); return opened

@app.get("/health")
def health() -> dict[str, Any]: return {"service": "wechat-collector", "status": "degraded" if bootstrap_error else "ok", "bound": bool(binding), "bootstrap_error": bootstrap_error}
@app.post("/bind")
def bind(req: BindRequest, x_collector_token: str | None = Header(default=None)) -> dict[str, Any]:
    auth(x_collector_token); global db, media, worker_thread
    try: opened = open_db(req.db_dir, req.wxid)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    with lock: db = opened; media = MediaDownloader(opened); binding.update(wxid=req.wxid, db_dir=str(Path(req.db_dir).resolve()), status="running", last_error=None); save_state()
    if worker_thread is None or not worker_thread.is_alive(): worker_thread = threading.Thread(target=collector_worker, name="wechat-collector-worker", daemon=True); worker_thread.start()
    return {"status": "running", **binding}
@app.post("/rebind")
def rebind(req: BindRequest, x_collector_token: str | None = Header(default=None)) -> dict[str, Any]: return bind(req, x_collector_token)
@app.get("/status")
def status(x_collector_token: str | None = Header(default=None)) -> dict[str, Any]: auth(x_collector_token); return {"status": binding.get("status", "stopped"), **binding}
@app.post("/stop")
def stop(x_collector_token: str | None = Header(default=None)) -> dict[str, str]: auth(x_collector_token); binding["status"] = "stopped"; save_state(); return {"status": "stopped"}
@app.get("/conversations")
def conversations(x_collector_token: str | None = Header(default=None)) -> dict[str, Any]:
    auth(x_collector_token)
    if db is None: return {"conversations": [], "total": 0}
    selected = set(config.get("selected_conversations") or []); items = []
    for row in db.get_sessions(limit=1000):
        cid = str(row.get("username") or row.get("chat_id") or "")
        if cid: items.append({"external_id": cid, "name": str(row.get("display_name") or row.get("name") or cid), "conversation_type": "group" if cid.endswith("@chatroom") else "private", "selected": cid in selected})
    return {"conversations": items, "total": len(items)}
@app.get("/contacts")
def contacts(keyword: str = "", x_collector_token: str | None = Header(default=None)) -> dict[str, Any]:
    auth(x_collector_token)
    if db is None: return {"contacts": [], "total": 0}
    rows = db.search_contact(keyword.strip()) if keyword.strip() else db.search_contact("")
    items = [{"username": str(row.get("username") or ""), "nick_name": str(row.get("nick_name") or ""), "remark": str(row.get("remark") or "")} for row in rows if str(row.get("username") or "").strip()]
    return {"contacts": items, "total": len(items)}
@app.get("/config")
def get_config(x_collector_token: str | None = Header(default=None)) -> dict[str, Any]: auth(x_collector_token); return config
@app.put("/config")
def save_config(value: dict[str, Any], x_collector_token: str | None = Header(default=None)) -> dict[str, Any]: auth(x_collector_token); config.update(value); save_state(); return config

if binding.get("status") == "running" and binding.get("db_dir") and binding.get("wxid"):
    try:
        db = open_db(str(binding["db_dir"]), str(binding["wxid"])); media = MediaDownloader(db)
        worker_thread = threading.Thread(target=collector_worker, name="wechat-collector-worker", daemon=True)
        worker_thread.start()
    except Exception as exc:
        binding["status"] = "error"; binding["last_error"] = str(exc)[:500]; save_state()

