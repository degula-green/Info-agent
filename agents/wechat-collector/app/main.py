import logging

from app.config import load_settings
from app.collector import WeChatAgent
from app.security import LocalDatabaseError


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()
    logging.info("Knowledge service: %s", settings.knowledge_base_url)
    try:
        agent = WeChatAgent(settings)
        agent.run_forever()
    except LocalDatabaseError:
        logging.error("WeChat collector stopped: local database validation failed")
        raise SystemExit(1)
    except Exception:
        logging.error("WeChat collector stopped")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
