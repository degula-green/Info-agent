"""Extraction result cache and call rate limiter."""

import threading
import time

from app.application.extraction_cache import CallRateLimiter, ExtractionCache


def test_same_prompt_is_served_from_the_cache():
    cache = ExtractionCache(max_entries=4)

    assert cache.get("窗口内容 A") is None
    cache.put("窗口内容 A", {"entities": [{"name": "张三"}]})

    assert cache.get("窗口内容 A") == {"entities": [{"name": "张三"}]}
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_a_different_prompt_is_a_miss():
    cache = ExtractionCache(max_entries=4)
    cache.put("窗口内容 A", {"entities": []})

    assert cache.get("窗口内容 B") is None
    assert cache.stats()["hits"] == 0


def test_cached_payload_cannot_be_corrupted_by_a_caller():
    # The scan hands the payload to the cleaning helpers; a mutation there must
    # not poison every later reuse of the entry.
    cache = ExtractionCache(max_entries=4)
    cache.put("p", {"entities": [{"name": "张三"}]})

    first = cache.get("p")
    first["entities"].append({"name": "篡改"})
    second = cache.get("p")

    assert [item["name"] for item in second["entities"]] == ["张三"]


def test_entries_beyond_the_bound_are_evicted_oldest_first():
    cache = ExtractionCache(max_entries=2)
    cache.put("p1", {"entities": [{"name": "一"}]})
    cache.put("p2", {"entities": [{"name": "二"}]})
    cache.put("p3", {"entities": [{"name": "三"}]})

    assert cache.size == 2
    assert cache.get("p1") is None
    assert cache.get("p3") is not None


def test_recently_used_entries_survive_eviction():
    cache = ExtractionCache(max_entries=2)
    cache.put("p1", {"entities": []})
    cache.put("p2", {"entities": []})
    cache.get("p1")  # p1 becomes the most recent
    cache.put("p3", {"entities": []})

    assert cache.get("p1") is not None
    assert cache.get("p2") is None


def test_hit_rate_over_an_empty_cache_is_zero():
    assert ExtractionCache().stats()["hit_rate"] == 0.0


def test_non_mapping_payloads_are_not_stored():
    cache = ExtractionCache()
    cache.put("p", "not a payload")

    assert cache.size == 0
    assert cache.get("p") is None


def test_concurrent_fills_do_not_lose_entries():
    cache = ExtractionCache(max_entries=200)

    def fill(start):
        for index in range(start, start + 50):
            cache.put(f"p{index}", {"entities": [{"name": str(index)}]})

    threads = [threading.Thread(target=fill, args=(n * 50,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert cache.size == 200


def test_zero_interval_does_not_wait():
    limiter = CallRateLimiter(min_interval_seconds=0.0)

    started = time.perf_counter()
    for _ in range(5):
        limiter.wait()

    assert time.perf_counter() - started < 0.05


def test_min_interval_spaces_call_starts():
    limiter = CallRateLimiter(min_interval_seconds=0.05)

    started = time.perf_counter()
    limiter.wait()
    limiter.wait()
    limiter.wait()

    # Three starts at 50ms spacing: the last lands ~100ms after the first.
    assert time.perf_counter() - started >= 0.08
