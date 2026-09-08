import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    knowledge_base_url: str
    pairing_id: str
    pairing_code: str
    wechat_id: str
    wechat_database_dir: str
    device_id: str
    device_key: str
    state_file: str
    agent_version: str
    poll_interval: float
    discovery_interval: float
    max_attachment_bytes: int


def load_settings() -> Settings:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    default_state_dir = Path(local_app_data) if local_app_data else Path.home() / ".info-agent"
    return Settings(
        knowledge_base_url=os.getenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:8090/api/knowledge/v1"),
        pairing_id=os.getenv("PAIRING_ID", ""),
        pairing_code=os.getenv("PAIRING_CODE", ""),
        wechat_id=os.getenv("WECHAT_ID", ""),
        wechat_database_dir=os.getenv("WECHAT_DATABASE_DIR", ""),
        device_id=os.getenv("WECHAT_DEVICE_ID", ""),
        device_key=os.getenv("WECHAT_DEVICE_KEY", ""),
        state_file=os.getenv("WECHAT_STATE_FILE", str(default_state_dir / "InfoAgent" / "wechat-agent-state.json")),
        agent_version=os.getenv("WECHAT_AGENT_VERSION", "wechat-collector/1.0"),
        poll_interval=max(1.0, float(os.getenv("WECHAT_POLL_INTERVAL", "10"))),
        discovery_interval=max(5.0, float(os.getenv("WECHAT_DISCOVERY_INTERVAL", "30"))),
        max_attachment_bytes=max(1, int(os.getenv("WECHAT_MAX_ATTACHMENT_BYTES", str(100 * 1024 * 1024)))),
    )
