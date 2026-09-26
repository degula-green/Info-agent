from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from typing import Any

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.embedding_api_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.embedding_api_key
        self.model = model or settings.embedding_model
        self.dimensions = dimensions or settings.embedding_dims
        self.http = http or HttpClient()
        self._cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.base_url or not self.api_key:
            raise RuntimeError("Embedding API is not configured")
        output: list[list[float]] = []
        missing: list[tuple[int, str, str]] = []
        now = time.monotonic()
        for index, text in enumerate(texts):
            key = self._cache_key(text)
            cached = self._get_cached(key, now)
            if cached is None:
                missing.append((index, text, key))
            else:
                while len(output) <= index:
                    output.append([])
                output[index] = cached
        if not missing:
            return output
        # The configured provider currently rejects requests larger than eight
        # inputs with HTTP 400. Keep the application setting as an upper bound
        # while enforcing the provider's documented-safe batch size.
        batch_size = min(max(1, settings.embedding_batch_size), 8)
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            response = self.http.request(
                "POST",
                join_url(self.base_url, "/embeddings"),
                # DashScope text-embedding-v4 defaults to a shorter vector.
                # Send the configured dimension explicitly so the response
                # matches the fixed ES vector mapping.
                body={"model": self.model, "input": [item[1] for item in batch], "dimensions": self.dimensions},
                token=self.api_key,
                timeout=settings.embedding_timeout_seconds,
            ).json()
            vectors = _extract_vectors(response)
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding API returned an unexpected number of vectors")
            for item, vector in zip(batch, vectors):
                _validate_vector(vector, self.dimensions)
                if settings.embedding_cache_enabled:
                    self._put_cached(item[2], vector, now)
                while len(output) <= item[0]:
                    output.append([])
                output[item[0]] = vector
        return output

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{self.dimensions}:{text}".encode("utf-8")).hexdigest()

    def _get_cached(self, key: str, now: float) -> list[float] | None:
        if not settings.embedding_cache_enabled:
            return None
        with self._cache_lock:
            value = self._cache.get(key)
            if value is None:
                return None
            expires_at, vector = value
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return list(vector)

    def _put_cached(self, key: str, vector: list[float], now: float) -> None:
        with self._cache_lock:
            self._cache[key] = (now + settings.embedding_cache_ttl_seconds, list(vector))
            self._cache.move_to_end(key)
            while len(self._cache) > 1024:
                self._cache.popitem(last=False)


class HashEmbeddingProvider:
    """Deterministic test double; never used by the production bootstrap."""

    def __init__(self, dimensions: int = 1536, model: str = "hash-test") -> None:
        self.dimensions = dimensions
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            for index, byte in enumerate(digest):
                vector[index % self.dimensions] += (byte - 127.5) / 127.5
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def _extract_vectors(value: Any) -> list[list[float]]:
    data = value.get("data", []) if isinstance(value, dict) else []
    if not isinstance(data, list):
        return []
    normalized = []
    for item in sorted(data, key=lambda entry: int(entry.get("index", 0)) if isinstance(entry, dict) else 0):
        vector = item.get("embedding") if isinstance(item, dict) else None
        normalized.append([float(number) for number in vector] if isinstance(vector, list) else [])
    return normalized


def _validate_vector(vector: list[float], dimensions: int) -> None:
    if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
        raise RuntimeError(f"embedding vector must contain exactly {dimensions} finite values")
