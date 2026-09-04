from app.config import settings


def search(query: str) -> dict[str, object]:
    return {
        "query": query,
        "index": settings.elasticsearch_url,
        "items": [],
    }
