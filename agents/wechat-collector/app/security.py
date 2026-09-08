from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any


class LocalDatabaseError(ValueError):
    pass


def validate_database_path(raw_path: str, wxid: str) -> tuple[Path, str, str]:
    """Validate a local WeChat database and return (root, account, fingerprint).

    The absolute path never leaves the agent. Only a SHA-256 fingerprint is sent
    to Knowledge during pairing.
    """
    value = str(raw_path or "").strip()
    identity = str(wxid or "").strip()
    if not value or not identity:
        raise LocalDatabaseError("WECHAT_DATABASE_DIR and WECHAT_ID are required")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.exists():
        raise LocalDatabaseError("wechat database path must be an existing absolute path")
    if path.is_file():
        storage = next((parent for parent in path.parents if parent.name == "db_storage"), None)
        if storage is None:
            raise LocalDatabaseError("database file must be located below an account db_storage directory")
        path = storage.parent
    if not path.is_dir():
        raise LocalDatabaseError("wechat database path must be a directory or database file")
    if (path / "db_storage").is_dir():
        account = path.name
        root = path.parent
    else:
        root = path
        candidates = sorted(item for item in root.iterdir() if item.is_dir() and (item / "db_storage").is_dir())
        normalized = re.sub(r"_\w{4}$", "", identity)
        matches = [item for item in candidates if re.sub(r"_\w{4}$", "", item.name) == normalized]
        selected = matches[0] if len(matches) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if selected is None:
            raise LocalDatabaseError("wechat database directory must contain one account matching WECHAT_ID")
        account = selected.name
    if not (root / account / "db_storage").is_dir():
        raise LocalDatabaseError("wechat database does not contain a readable db_storage directory")
    resolved = (root / account).resolve()
    fingerprint = hashlib.sha256(os.fsencode(str(resolved).lower())).hexdigest()
    return root.resolve(), account, fingerprint


def payload_hash(value: Any) -> str:
    """Hash a canonical business payload, excluding its self-referential field."""
    payload = dict(value) if isinstance(value, dict) else value
    if isinstance(payload, dict):
        payload.pop("payload_hash", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # Match Go's encoding/json with SetEscapeHTML(false), which still escapes
    # the two line-separator code points for safe JavaScript embedding.
    raw = raw.replace("\u2028".encode("utf-8"), b"\\u2028").replace("\u2029".encode("utf-8"), b"\\u2029")
    return hashlib.sha256(raw).hexdigest()


def content_hash(stream, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def signature(device_key: str, timestamp: str, method: str, path: str, body_hash: str) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n{body_hash}".encode("utf-8")
    return hmac.new(device_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
