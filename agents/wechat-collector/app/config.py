import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    knowledge_base_url: str
    wechat_id: str
    wechat_database_dir: str


def load_settings() -> Settings:
    return Settings(
        knowledge_base_url=os.getenv("KNOWLEDGE_BASE_URL", "http://127.0.0.1:8090"),
        wechat_id=os.getenv("WECHAT_ID", ""),
        wechat_database_dir=os.getenv("WECHAT_DATABASE_DIR", ""),
    )
