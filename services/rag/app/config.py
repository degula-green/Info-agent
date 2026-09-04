import os


class Settings:
    elasticsearch_url: str = os.getenv("ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    elasticsearch_username: str = os.getenv("ELASTICSEARCH_USERNAME", "")
    elasticsearch_password: str = os.getenv("ELASTICSEARCH_PASSWORD", "")
    elasticsearch_index: str = os.getenv("ELASTICSEARCH_INDEX", "info-agent-documents")
    elasticsearch_verify_certs: bool = os.getenv("ELASTICSEARCH_VERIFY_CERTS", "false").lower() == "true"


settings = Settings()
