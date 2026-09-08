import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    database_url: str
    token: str
    port: int

def load() -> Settings:
    return Settings(os.getenv('COLLECTOR_DATABASE_URL',''), os.getenv('COLLECTOR_INTERNAL_TOKEN','local-development-only'), int(os.getenv('WECHAT_COLLECTOR_PORT','8091')))
