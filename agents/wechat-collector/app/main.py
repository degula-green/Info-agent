import logging

from app.config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()
    logging.info("Knowledge service: %s", settings.knowledge_base_url)
    if not settings.wechat_id or not settings.wechat_database_dir:
        logging.warning("WeChat collector configuration is incomplete")
    logging.info("WeChat collector is not implemented; no data will be read")


if __name__ == "__main__":
    main()
