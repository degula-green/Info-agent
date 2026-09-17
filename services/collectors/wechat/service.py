from __future__ import annotations
import hashlib, html, json, mimetypes, os, re, shutil, tempfile, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from wechatauto import MediaDownloader, WeChatDB

app = FastAPI(title="info-agent-wechat-collector")
lock = threading.Lock(); binding: dict[str, Any] = {}; db: Any = None
config: dict[str, Any] = {"selected_conversations": [], "history_start_at": None, "enabled": True, "listen_mode": "whitelist", "connector_id": ""}
checkpoints: dict[str, int] = {}; replayed_media: dict[str, set[str]] = {}; worker_thread: threading.Thread | None = None; discovery_thread: threading.Thread | None = None
media: Any = None; bootstrap_error: str | None = None
last_discovery_at = 0.0; last_bootstrap_at = 0.0
discovery_lock = threading.Lock()

class BindRequest(BaseModel):
    wxid: str = Field(min_length=3)
    db_dir: str = Field(min_length=3)

def auth(token: str | None) -> None:
    if token != os.getenv("COLLECTOR_INTERNAL_TOKEN", "local-development-only"): raise HTTPException(401, "invalid collector credential")

def state_path() -> Path: return Path(os.getenv("WECHAT_COLLECTOR_STATE_FILE", "./data/wechat-collector.json")).expanduser()
def load_state() -> None:
    global checkpoints, replayed_media
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        checkpoints = {str(k): int(v) for k, v in (value.get("checkpoints") or {}).items()}
        replayed_media = {str(k): {str(item) for item in (items or [])} for k, items in (value.get("replayed_media") or {}).items()}
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

def ensure_bootstrap() -> bool:
    """Retry Knowledge bootstrap after startup ordering or transient outages."""
    global db, media, last_bootstrap_at, bootstrap_error
    if binding.get("status") == "running" and db is not None and config.get("connector_id"):
        return True
    now = time.time()
    retry_interval = max(1.0, float(os.getenv("WECHAT_BOOTSTRAP_RETRY_INTERVAL", "5")))
    if now - last_bootstrap_at < retry_interval:
        return False
    last_bootstrap_at = now
    bootstrap_from_knowledge()
    if binding.get("status") != "running" or not binding.get("db_dir") or not binding.get("wxid"):
        return False
    try:
        opened = open_db(str(binding["db_dir"]), str(binding["wxid"]))
        with lock:
            db = opened
            media = MediaDownloader(opened)
        bootstrap_error = None
        return True
    except Exception as exc:
        binding["status"] = "error"
        binding["last_error"] = str(exc)[:500]
        bootstrap_error = f"WeChat database bootstrap failed: {exc}"[:500]
        return False

def save_state() -> None:
    path = state_path(); path.parent.mkdir(parents=True, exist_ok=True); temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"checkpoints": checkpoints, "replayed_media": {key: sorted(values) for key, values in replayed_media.items()}}, ensure_ascii=False), encoding="utf-8"); temp.replace(path)
def knowledge(path: str, method: str = "GET", payload: Any = None) -> dict[str, Any]:
    base = os.getenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:8090").rstrip("/"); body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    token = os.getenv("KNOWLEDGE_INTERNAL_SERVICE_TOKEN", "local-development-only")
    req = urllib.request.Request(base + path, data=body, method=method, headers={"Accept": "application/json", "Content-Type": "application/json", "X-Service-Token": token})
    with urllib.request.urlopen(req, timeout=30) as response: return json.loads(response.read().decode("utf-8") or "{}")

def ingest_message(collector_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Ingest one message without allowing an idempotency conflict to starve other chats."""
    try:
        return knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/messages", "POST", payload)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        try:
            code = str((json.loads(body) or {}).get("code") or "")
        except (TypeError, ValueError):
            code = ""
        if exc.code == 409 and code == "external_id_conflict":
            return None
        raise

load_state()
bootstrap_from_knowledge()

def normalized_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("message_type") or "text").lower()
    content = media_payload(raw)
    if re.search(r"<img\b", content, re.I): return "image"
    if re.search(r"<appmsg\b[\s\S]*?<type>\s*6\s*</type>", content, re.I): return "file"
    if value in {"49", "文件/链接/卡片"}:
        return "file" if is_file_payload(str(raw.get("content") or "")) else "text"
    mapped = {
        "49": "file", "3": "image", "43": "video", "video": "video", "10000": "system",
        "文本": "text", "图片": "image", "视频": "video", "文件": "file",
    }
    return mapped.get(value, value if value in {"text", "image", "file", "video", "mixed", "system"} else "text")

def is_file_payload(content: str) -> bool:
    # WeChat stores files, links, and mini-program cards under one type. A
    # real appmsg type=6 or an explicit file name/extension distinguishes a
    # real file from a forwarded chat record, link card, or mini-program.
    content = decode_xml(content)
    if re.search(r"<appmsg\b[^>]*>[\s\S]*?<type>\s*6\s*</type>", content, re.I):
        return True
    return any(re.search(rf"<{tag}\b[^>]*>\s*[^<\s][^<]*?\s*</{tag}>", content, re.I | re.S) for tag in ("fileext", "filename"))

def media_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("message_type") or "").lower()
    content = media_payload(raw)
    if re.search(r"<img\b", content, re.I): return "image"
    if re.search(r"<appmsg\b[\s\S]*?<type>\s*6\s*</type>", content, re.I): return "file"
    if value in {"43", "video", "视频"}: return "video"
    if value in {"3", "image", "图片"}: return "image"
    if value in {"file", "文件"}: return "file"
    if value == "49":
        return "file" if is_file_payload(content) else ""
    if value == "文件/链接/卡片":
        # WeChat uses one DB type for files, links, and cards. Only XML
        # payloads carrying a file marker should create an attachment.
        if is_file_payload(content):
            return "file"
        return ""
    return normalized_type(raw)

def decode_xml(content: Any) -> str:
    """Decode WeChat's nested HTML-escaped XML payloads."""
    value = str(content or "")
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value

def media_payload(raw: dict[str, Any]) -> str:
    """Return the innermost media XML for direct and forwarded messages."""
    content = decode_xml(raw.get("content"))
    # Forwarded messages use an outer appmsg type=57 and put the original
    # message in refermsg/content. Use it for attachment classification while
    # leaving the outer message text untouched for display.
    nested = re.findall(r"<refermsg\b[\s\S]*?<content>([\s\S]*?)</content>", content, re.I)
    for candidate in reversed(nested):
        decoded = decode_xml(candidate)
        if re.search(r"<(?:msg|appmsg|img|emoji)\b", decoded, re.I):
            return decoded
    return content

def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value); number /= 1000 if number > 10_000_000_000 else 1
        return datetime.fromtimestamp(number, timezone.utc)
    text = str(value or "").strip()
    # SQLite/provider adapters may return Unix timestamps as strings. Treat
    # them exactly like numeric values instead of falling back to now(), which
    # makes historical messages appear to have been sent during collection.
    try:
        number = float(text)
        number /= 1000 if number > 10_000_000_000 else 1
        return datetime.fromtimestamp(number, timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try: return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError): return datetime.now(timezone.utc)

def payload_hash(value: dict[str, Any]) -> str:
    raw = json.dumps({key: item for key, item in value.items() if key != "payload_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw = raw.replace("\u2028".encode("utf-8"), b"\\u2028").replace("\u2029".encode("utf-8"), b"\\u2029")
    return hashlib.sha256(raw).hexdigest()

def nickname_index() -> dict[str, str]:
    """Return the local WeChat contact display-name index when available."""
    if db is None:
        return {}
    try:
        # The library's helper prefers ``remark``.  A remark is an internal
        # label and often contains a group prefix or phone number; message
        # rows should use the contact's actual WeChat nickname first.
        for rel, path, _ in db._db_files:
            if Path(path).name != "contact.db":
                continue
            conn = db._open(rel)
            try:
                return {
                    str(username): str(nick_name or remark or username)
                    for username, nick_name, remark in conn.execute(
                        "SELECT username, nick_name, remark FROM contact"
                    )
                    if username
                }
            finally:
                conn.close()
        return {}
    except Exception:
        return {}

def display_name(identifier: Any, names: dict[str, str] | None = None) -> str:
    value = str(identifier or "").strip()
    if not value:
        return "未知成员"
    names = names if names is not None else nickname_index()
    return names.get(value) or value

def is_raw_wechat_id(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@chatroom)", text, re.I))

def same_wechat_account(left: Any, right: Any) -> bool:
    """Compare WeChat account ids, including desktop account suffixes."""
    first = str(left or "").strip().lower()
    second = str(right or "").strip().lower()
    if not first or not second:
        return False
    return first == second or first.startswith(second + "_") or second.startswith(first + "_")

def strip_sender_prefix(content: Any, sender_external_id: Any = "") -> str:
    """Remove the sender marker that some WeChat DB readers prepend to content."""
    value = str(content or "").strip()
    candidates = [str(sender_external_id or "").strip()]
    for candidate in candidates:
        if candidate:
            value = re.sub(rf"^{re.escape(candidate)}\s*:\s*", "", value, count=1, flags=re.I)
    # Keep this fallback deliberately narrow so normal text such as "Note: ..."
    # is not changed, while wxid/chatroom markers from historical rows are fixed.
    return re.sub(r"^(?:wxid_[A-Za-z0-9_-]+(?:@chatroom)?|[A-Za-z0-9_-]+@chatroom)\s*:\s*", "", value, count=1, flags=re.I)

def resolve_sender(
    raw: dict[str, Any],
    names: dict[str, str],
    *,
    conversation_type: str = "group",
    conversation_id: str = "",
    conversation_name: str = "",
) -> tuple[str, str]:
    """Resolve the actual group sender before trusting DB's resource sender.

    WeChat 4.x stores the message resource sender separately from the sender
    encoded in group-message content. The latter is authoritative for group
    rows, including XML messages which expose ``fromusername``.
    """
    content = str(raw.get("content") or "")
    prefix = re.match(r"^\s*(wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@chatroom)\s*:\s*", content, re.I)
    parsed_sender = prefix.group(1) if prefix else ""
    if not parsed_sender:
        match = re.search(r"<fromusername\b[^>]*>\s*(?:<!\[CDATA\[)?([^<\]]+)", content, re.I)
        if not match:
            match = re.search(r"<(?:emoji|img)\b[^>]*\bfromusername\s*=\s*[\"']([^\"']+)", content, re.I)
        parsed_sender = match.group(1).strip() if match else ""
    provider_sender = str(raw.get("sender_username") or raw.get("sender_id") or binding.get("wxid", "")).strip()
    sender = parsed_sender or provider_sender

    is_private = str(conversation_type).lower() == "private"
    if is_private and str(conversation_id or "").strip():
        # The WeChat 4.x reader can expose a stale sender_username for private
        # rows. XML sender markers are authoritative; plain-text rows from the
        # other party have no marker, so the conversation id is the only stable
        # identity. Keep the local account id for messages sent by ourselves.
        account_id = str(binding.get("wxid") or "").strip()
        if parsed_sender:
            sender = parsed_sender
        elif provider_sender and same_wechat_account(provider_sender, account_id):
            sender = provider_sender
        else:
            sender = str(conversation_id).strip()

    resolved = display_name(sender, names)
    # In a private chat every non-self message belongs to the conversation's
    # counterpart, even when an old row contains a stale but human-looking
    # sender display name. Group messages remain member-specific.
    if (
        is_private
        and not same_wechat_account(sender, binding.get("wxid"))
        and str(conversation_name or "").strip()
        and not is_raw_wechat_id(conversation_name)
    ):
        resolved = str(conversation_name).strip()
    return sender, resolved

def message_content(raw: dict[str, Any], attachments: list[dict[str, Any]], sender_external_id: str = "") -> str:
    """Return user-authored text without provider media envelopes.

    WeChat stores images/files as XML in the same column used for text.  The
    attachment metadata is persisted separately, so the XML envelope (and its
    filename/title) must never become a visible message body.  Plain captions
    remain intact.
    """
    value = strip_sender_prefix(raw.get("content"), sender_external_id).strip()
    if not value or not attachments:
        return value
    if re.match(r"^\s*(?:<\?xml\b[\s\S]*?\?>\s*)?<msg\b", value, re.I):
        return ""
    if re.match(r"^\s*\{[\s\S]*\}\s*$", value):
        try:
            payload = json.loads(value)
            if isinstance(payload, dict) and any(
                re.fullmatch(r"(?:file|image)_(?:key|token|name)|filename", str(key), re.I)
                for key in payload
            ):
                return ""
        except (TypeError, ValueError):
            pass
    attachment_names = {str(item.get("file_name") or "").strip().casefold() for item in attachments}
    if value.casefold() in attachment_names or value.casefold() in {"filename", "file name"}:
        return ""
    return value

def attachment_metadata(chat_id: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    kind = media_type(raw)
    if kind not in {"file", "image", "video"}: return []
    local_id = str(raw.get("local_id") or "0"); content = media_payload(raw)
    name = attachment_file_name(raw, kind)
    mime = ("image/jpeg" if kind == "image" and name == "image.bin" else mimetypes.guess_type(name)[0]) or ("video/mp4" if kind == "video" else "application/octet-stream")
    size_match = re.search(r"<(?:totallen|datasize)>\s*(\d+)\s*</(?:totallen|datasize)>", content, re.I)
    size = int(size_match.group(1)) if size_match else 0
    return [{"external_attachment_id": f"{binding.get('wxid','')}:{chat_id}:{local_id}:0", "file_name": name, "mime_type": mime, "size_bytes": size, "content_hash": ""}]

def attachment_file_name(raw: dict[str, Any], kind: str) -> str:
    content = media_payload(raw)
    matches = [re.search(pattern, content, re.I | re.S) for pattern in (r"<filename>\s*([^<]+?)\s*</filename>", r"<datatitle>\s*([^<]+?)\s*</datatitle>", r"<title>\s*([^<]+?)\s*</title>")]
    match = next((item for item in matches if item and item.group(1).strip()), None)
    fallback = "image.bin" if kind == "image" else "video.mp4" if kind == "video" else "attachment.bin"
    name = Path((match.group(1) if match else fallback)).name[:255]
    if kind == "file" and name == "attachment.bin":
        ext = re.search(r"<fileext>\s*([a-z0-9]+)\s*</fileext>", content, re.I)
        if ext: name = f"attachment.{ext.group(1).lower()}"
    return name

def local_file_fallback(raw: dict[str, Any], root: Path) -> str | None:
    kind = media_type(raw)
    if kind != "file":
        return None
    name = attachment_file_name(raw, kind)
    account_dir = Path(getattr(db, "account_dir", ""))
    month = parse_time(raw.get("create_time")).strftime("%Y-%m")
    search_roots = [account_dir / "msg" / "file" / month, account_dir / "msg" / "file"]
    candidate = next((path for search_root in search_roots if search_root.is_dir() for path in search_root.rglob(name) if path.is_file()), None)
    if candidate is None:
        # WeChat appends "(1)", "(2)", ... when the same file name is
        # downloaded more than once. Match only that exact duplicate form and
        # prefer the newest copy, avoiding similarly named unrelated files.
        requested = Path(name)
        duplicate_pattern = re.compile(rf"^{re.escape(requested.stem)}\(\d+\){re.escape(requested.suffix)}$", re.I)
        duplicates = [
            path
            for search_root in search_roots
            if search_root.is_dir()
            for path in search_root.rglob("*")
            if path.is_file() and duplicate_pattern.match(path.name)
        ]
        if duplicates:
            candidate = max(duplicates, key=lambda path: path.stat().st_mtime)
    if candidate is None:
        return None
    fallback = root / name
    shutil.copyfile(candidate, fallback)
    return str(fallback)

def download_attachment(chat_id: str, raw: dict[str, Any]) -> tuple[Path, Path] | None:
    if media is None: return None
    try: local_id = int(raw.get("local_id"))
    except (TypeError, ValueError): return None
    root = Path(tempfile.mkdtemp(prefix="wechat-attachment-"))
    try:
        kind = media_type(raw)
        if kind == "file":
            local = local_file_fallback(raw, root)
            if local:
                return Path(local).resolve(), root.resolve()
        method = "download_image" if kind == "image" else "download_video" if kind == "video" else "download_file"
        result = getattr(media, method)(chat_id, local_id, str(root))
        if not result or not Path(result).is_file():
            if kind == "file":
                result = local_file_fallback(raw, root)
            if not result or not Path(result).is_file():
                shutil.rmtree(root, ignore_errors=True); return None
        path = Path(result).resolve(); resolved_root = root.resolve()
        if resolved_root not in path.parents or path.stat().st_size > int(os.getenv("WECHAT_MAX_ATTACHMENT_BYTES", str(512 * 1024 * 1024))):
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
    token = os.getenv("KNOWLEDGE_INTERNAL_SERVICE_TOKEN", "local-development-only")
    request = urllib.request.Request(f"{base}/api/knowledge/v1/internal/collectors/{collector_id}/attachments", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Service-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response: return json.loads(response.read().decode() or "{}")

def messages_after(db_instance: Any, chat_id: str, since: int, limit: int = 200) -> list[dict[str, Any]]:
    """Read an incremental page, tolerating a concurrently rewritten shard.

    WeChat may rewrite one encrypted message shard while the desktop client is
    running. The provider reader can then fail while locating the table even
    though the merged historical database remains readable. Falling back to a
    bounded historical page keeps collection progressing and preserves the
    cursor contract; the next poll will retry any rows that were not returned.
    """
    if not since:
        return list(reversed(db_instance.get_messages(chat_id, limit=1000, offset=0)))
    try:
        return db_instance.get_new_messages(chat_id, since_seq=since, limit=limit)
    except Exception as first_error:
        try:
            historical = db_instance.get_messages(chat_id, limit=max(1000, limit), offset=0)
        except Exception:
            raise first_error
        rows = []
        for row in historical:
            try:
                sequence = int(row.get("sort_seq") or row.get("local_id") or 0)
            except (TypeError, ValueError):
                continue
            if sequence > since:
                rows.append(row)
        rows.sort(key=lambda row: int(row.get("sort_seq") or row.get("local_id") or 0))
        return rows[:limit]

def collect_once() -> None:
    if binding.get("status") == "running" and config.get("enabled") and config.get("connector_id") and db is not None:
        assignments = knowledge("/api/knowledge/v1/internal/wechat/assignments?connector_id=" + str(config["connector_id"])).get("items", [])
        selected = set(config.get("selected_conversations") or [])
        # An attached active conversation is already an explicit collection
        # choice. Keep it collectable even when an older whitelist snapshot
        # predates the attachment (notably for private chats).
        if config.get("listen_mode") == "whitelist":
            selected.update(
                str((item.get("conversation") or {}).get("external_conversation_id") or "")
                for item in assignments
                if (item.get("conversation") or {}).get("status") == "active"
            )
        names = nickname_index()
        for item in assignments:
            conversation = item.get("conversation") or {}; collector = item.get("collector") or {}; chat_id = str(conversation.get("external_conversation_id") or ""); collector_id = str(collector.get("id") or "")
            if conversation.get("status") != "active" or not chat_id or not collector_id or (config.get("listen_mode") == "whitelist" and chat_id not in selected): continue
            try: since = int(str(collector.get("last_cursor") or "0"))
            except ValueError: since = checkpoints.get(collector_id, 0)
            rows = messages_after(db, chat_id, since, limit=200)
            start_at = conversation.get("effective_start_at") or conversation.get("requested_start_at") or config.get("history_start_at")
            if not since and start_at: rows = [row for row in rows if parse_time(row.get("create_time")) >= parse_time(start_at)]
            # Reconcile media rows already ingested before the provider-aware
            # parser was introduced. WeChat stores forwarded files as type=57;
            # replaying only rows that now classify as media lets Knowledge
            # attach the recovered metadata without moving the cursor back.
            replay_ids: set[str] = set()
            if since:
                seen_ids = {str(row.get("local_id") or row.get("server_id") or row.get("sort_seq") or "") for row in rows}
                already_replayed = replayed_media.get(collector_id, set())
                replay_limit = max(0, int(os.getenv("WECHAT_MEDIA_REPLAY_BATCH_SIZE", "10")))
                for candidate in db.get_messages(chat_id, limit=1000, offset=0):
                    candidate_id = str(candidate.get("local_id") or candidate.get("server_id") or candidate.get("sort_seq") or "")
                    if candidate_id and candidate_id not in seen_ids and candidate_id not in already_replayed and media_type(candidate) in {"file", "image", "video"}:
                        rows.append(candidate)
                        replay_ids.add(candidate_id)
                        if len(replay_ids) >= replay_limit:
                            break
            for raw in rows:
                local_id = str(raw.get("local_id") or raw.get("server_id") or raw.get("sort_seq") or "0")
                cursor = str(raw.get("sort_seq") or raw.get("local_id") or "")
                attachments = attachment_metadata(chat_id, raw)
                conversation_type = str(
                    conversation.get("conversation_type")
                    or ("group" if chat_id.endswith("@chatroom") else "private")
                )
                sender_external_id, sender_display_name = resolve_sender(
                    raw,
                    names,
                    conversation_type=conversation_type,
                    conversation_id=chat_id,
                    conversation_name=str(conversation.get("name") or ""),
                )
                content = message_content(raw, attachments, sender_external_id)
                value = {
                    "collector_id": collector_id,
                    "external_conversation_id": chat_id,
                    "external_message_id": f"{binding.get('wxid', '')}:{chat_id}:{local_id}",
                    "sender_external_id": sender_external_id,
                    "sender_display_name": sender_display_name,
                    "message_type": normalized_type(raw),
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                    "sent_at": parse_time(raw.get("create_time")).isoformat().replace("+00:00", "Z"),
                    "cursor": cursor,
                    "attachments": attachments,
                }
                value["payload_hash"] = payload_hash(value)
                result = ingest_message(collector_id, value)
                if result is not None:
                    saved_attachments = result.get("attachments") or result.get("message", {}).get("attachments") or []
                    if len(saved_attachments) != len(attachments):
                        raise RuntimeError("knowledge returned incomplete attachment metadata")
                    for attachment, saved in zip(attachments, saved_attachments):
                        if str(saved.get("content_status") or "") == "ready":
                            continue
                        downloaded = download_attachment(chat_id, raw)
                        # Metadata is still useful when WeChat no longer has the
                        # local blob (common for old forwarded images). Keep the
                        # message/cursor moving and let the attachment remain
                        # pending for a later replay instead of wedging the whole
                        # collector.
                        if not downloaded:
                            continue
                        path, temp_root = downloaded
                        try:
                            digest = hashlib.sha256(path.read_bytes()).hexdigest()
                            attachment["content_hash"] = digest
                            upload_attachment(
                                collector_id,
                                str(saved.get("id") or attachment["external_attachment_id"]),
                                path,
                                attachment["file_name"],
                                attachment["mime_type"],
                                digest,
                            )
                        finally:
                            shutil.rmtree(temp_root, ignore_errors=True)
                if local_id not in replay_ids:
                    knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/cursor-receipt", "POST", {"cursor": cursor})
                    knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/cursor", "POST", {"cursor": cursor})
                else:
                    replayed_media.setdefault(collector_id, set()).add(local_id)
                try:
                    numeric_cursor = int(cursor or 0)
                except ValueError:
                    numeric_cursor = checkpoints.get(collector_id, 0)
                checkpoints[collector_id] = max(checkpoints.get(collector_id, 0), numeric_cursor)
                save_state()
            knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/heartbeat", "POST", {"agent_version": "server-wechat-collector"})
        # A complete poll supersedes a transient error from an earlier shard
        # rewrite or attachment retry; keep status consumers from showing a
        # stale failure after the collector has recovered.
        binding.pop("last_error", None)

def discover_conversations() -> dict[str, Any]:
    connector_id = str(binding.get("connector_id") or config.get("connector_id") or "").strip()
    if db is None or not connector_id:
        return {"items": []}
    items = []; names = nickname_index()
    include_members = os.getenv("WECHAT_DISCOVERY_INCLUDE_MEMBERS", "1").strip().lower() in {"1", "true", "yes", "on"}
    for row in db.get_sessions(limit=1000):
        external_id = str(row.get("username") or row.get("chat_id") or "").strip()
        if not external_id:
            continue
        name = names.get(external_id) or str(row.get("display_name") or row.get("name") or "").strip() or external_id
        # Member expansion is expensive on large local WeChat databases. Only
        # expand groups already selected for collection; unselected groups are
        # still discoverable immediately and get their memberships when the
        # user enables them.
        members = group_members(external_id) if include_members and external_id.endswith("@chatroom") else []
        items.append({
            "external_id": external_id,
            "name": name,
            "conversation_type": "group" if external_id.endswith("@chatroom") else "private",
            "member_count": len(members),
            "members": members,
            "metadata": {"source": "wechat_collector"},
        })
    return knowledge("/api/knowledge/v1/internal/wechat/discovery", "POST", {"connector_id": connector_id, "items": items})

def refresh_discovery() -> None:
    """Refresh discovery without blocking message collection or UI requests."""
    global discovery_thread
    with discovery_lock:
        if discovery_thread is not None and discovery_thread.is_alive():
            return
        def run() -> None:
            global discovery_thread
            try:
                discover_conversations()
            except Exception as exc:
                binding["last_error"] = f"discovery failed: {exc}"[:500]
            finally:
                with discovery_lock:
                    discovery_thread = None
        discovery_thread = threading.Thread(target=run, name="wechat-discovery", daemon=True)
        discovery_thread.start()

def group_members(chat_id: str) -> list[dict[str, str]]:
    """Read the contact.db chatroom membership relation for an exact count."""
    if db is None or not chat_id.endswith("@chatroom"):
        return []
    try:
        for rel, path, _ in db._db_files:
            if Path(path).name != "contact.db":
                continue
            conn = db._open(rel)
            try:
                room = conn.execute("SELECT id FROM chat_room WHERE username=? LIMIT 1", (chat_id,)).fetchone()
                if not room:
                    return []
                rows = conn.execute("SELECT c.username,c.nick_name,c.remark FROM chatroom_member m JOIN contact c ON c.id=m.member_id WHERE m.room_id=?", (room["id"],)).fetchall()
                return [{"external_user_id": str(row["username"]), "display_name": str(row["nick_name"] or row["remark"] or row["username"])} for row in rows if row["username"]]
            finally:
                conn.close()
    except Exception:
        return []
    return []

def collector_worker() -> None:
    global last_discovery_at
    while True:
        try:
            if not ensure_bootstrap():
                time.sleep(float(os.getenv("WECHAT_COLLECTOR_POLL_INTERVAL", "5")))
                continue
            if time.time() - last_discovery_at >= float(os.getenv("WECHAT_DISCOVERY_INTERVAL", "60")):
                refresh_discovery(); last_discovery_at = time.time()
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
    refresh_discovery()
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
    selected = set(config.get("selected_conversations") or []); items = []; names = nickname_index()
    for row in db.get_sessions(limit=1000):
        cid = str(row.get("username") or row.get("chat_id") or "")
        if cid: items.append({"external_id": cid, "name": names.get(cid) or str(row.get("display_name") or row.get("name") or cid), "conversation_type": "group" if cid.endswith("@chatroom") else "private", "selected": cid in selected})
    refresh_discovery()
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
        refresh_discovery()
    except Exception as exc:
        binding["status"] = "error"; binding["last_error"] = str(exc)[:500]; save_state()

# Always keep a recovery loop alive. Knowledge may still be starting when this
# module is imported, and ensure_bootstrap handles that dependency ordering.
worker_thread = threading.Thread(target=collector_worker, name="wechat-collector-worker", daemon=True)
worker_thread.start()

