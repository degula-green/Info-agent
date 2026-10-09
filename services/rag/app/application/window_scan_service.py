"""Window scan: mount implicit-topic messages onto entities.

Messages keep flowing through traditional RAG while this worker runs behind
them. It groups a conversation into overlapping windows, asks the extraction
model which entities and relations the window is about, and writes the result
as mounts. Nothing here may block ingestion, so failures leave the watermark
untouched and the next sweep retries.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.application.entity_service import EntityMatcher, match_chunk_mounts
from app.application.extraction_cache import CallRateLimiter, ExtractionCache
from app.config import settings
from app.domain.rag import Chunk, Entity, EntityAlias, EntityMount, normalized_text
from app.infrastructure.extraction.client import EntityExtractionClient, ExtractionError


logger = logging.getLogger("rag.window-scan")


ALLOWED_DOMAINS = {"organization", "person", "project", "policy", "contract"}
ALLOWED_RELATIONS = {
    "works_for", "participates_in", "belongs_to", "governed_by",
    "signed_by", "related_to", "applies_to", "contacts",
}

# Mount confidence tiers. Explicit is set by the text matcher; the others come
# from the model's own confidence, which is why strong and weak are distinct.
MOUNT_CONFIDENCE = {
    "explicit": 0.95,
    "window_strong": 0.85,
    "window_weak": 0.65,
    "llm_infer": 0.50,
}

# Credentials are not entities, and a password written into a candidate row or
# an entity name is a leak that survives review. Filter them out of the model
# output rather than trusting the prompt alone.
_CREDENTIAL_PATTERN = re.compile(
    r"(密码|口令|密钥|验证码|身份证|银行卡|password|passwd|secret|api[-_ ]?key|token)",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{8,}")
_GENERIC_SUBJECT_PATTERN = re.compile(
    r"^(?:(?:新|new|最新|新版|旧版|当前|现有|测试|正式|第一|第二|第三|第四|第五|mvp|beta|v\d+(?:\.\d+)*)\s*)?"
    r"(?:版|版本|版?app|应用|系统|平台|模块|功能|文档|计划|方案|安装包|页面|服务)$",
    re.IGNORECASE,
)

_WINDOW_PROMPT = """## 任务
从对话中提取实体（公司、人名、项目、政策、合同），以及实体之间的关系。

## 对话内容
{window}

## 提取规则
1. 提取所有被实际讨论的具体实体（不要泛指）
2. 包括简称、昵称（如"aims"、"小张"）
3. 给出你认为的规范名称
4. 类型必须从 organization|person|project|policy|contract 中选一个；无法判断时丢弃
5. 设备、服务器、数据库实例、云服务产品、账号、金额、时间、地址不作为实体提取；
   泛化的版本、发布物、功能、模块、文档名称（如“MVP版本”“新版app”“第一版安装包”）
   也不是实体。若上下文已经给出所属项目或组织，只提取该项目/组织，不要为它的版本、
   发布物或功能另建实体
6. 凭据类信息（账号、密码、token、密钥、验证码、身份证号、银行卡号）一律不要提取，
   也不要写进 evidence
7. 关系类型必须从以下枚举选一个，不要自创：
   works_for | participates_in | belongs_to | governed_by |
   signed_by | related_to | applies_to | contacts
8. 只在关系稳定明确时输出，不要把"同一次对话里提到"当成关系

## 输出格式
只返回 JSON 对象：
{{"entities":[{{"name":"","type":"","confidence":0.0,"mentions":[],"evidence":""}}],
  "relations":[{{"source":"","target":"","type":"","confidence":0.0}}]}}
"""


@dataclass
class WindowResult:
    """Per-window counters.

    Returned rather than accumulated into a shared object so windows can run
    concurrently without a lock around the totals.
    """

    mounts: int = 0
    candidates: int = 0
    relations: int = 0
    # How many entities the model returned for this window, before resolution.
    # Zero is the signal that matters: it can mean "nothing here" or "the model
    # skipped it", and only the rate over time tells the two apart.
    entities: int = 0


@dataclass
class WindowScanOutcome:
    conversations: int = 0
    windows: int = 0
    empty_windows: int = 0
    mounts: int = 0
    candidates: int = 0
    relations: int = 0
    failed_conversations: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversations": self.conversations,
            "windows": self.windows,
            "empty_windows": self.empty_windows,
            "mounts": self.mounts,
            "candidates": self.candidates,
            "relations": self.relations,
            "failed_conversations": self.failed_conversations,
            "errors": self.errors[:5],
        }


class EntityWindowScanWorker:
    """Scan conversations that have messages past their watermark."""

    def __init__(
        self,
        *,
        repository: Any,
        extractor: EntityExtractionClient | Any,
        window_size: int | None = None,
        window_step: int | None = None,
        max_chunks_per_conversation: int = 500,
        concurrency: int | None = None,
        cache: ExtractionCache | None = None,
        rate_limiter: CallRateLimiter | None = None,
        indexer: Any | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.indexer = indexer
        self.window_size = max(2, int(window_size or settings.extract_window_size))
        self.window_step = max(1, int(window_step or settings.extract_window_step))
        self.max_chunks_per_conversation = max(1, int(max_chunks_per_conversation))
        # Only one conversation at a time, but its windows are independent IO
        # waits, so they overlap up to this bound.
        self.concurrency = max(1, int(concurrency or settings.extract_concurrency))
        if cache is not None:
            self.cache = cache
        elif settings.extract_cache_enabled:
            self.cache = ExtractionCache(max_entries=settings.extract_cache_size)
        else:
            self.cache = None
        self.rate_limiter = rate_limiter or CallRateLimiter(
            min_interval_seconds=settings.extract_min_interval_ms / 1000.0
        )

    def run_once(self, *, conversation_limit: int = 5) -> WindowScanOutcome:
        outcome = WindowScanOutcome()
        per_scope: dict[tuple[str, str], WindowScanOutcome] = {}
        for conversation in self.repository.list_scan_conversations(limit=conversation_limit):
            key = (conversation["scope_type"], conversation["scope_id"])
            scoped = per_scope.setdefault(key, WindowScanOutcome())
            try:
                self._scan_conversation(conversation, scoped)
                scoped.conversations += 1
            except Exception as exc:
                # The watermark stays put, so the next sweep picks this
                # conversation up again instead of losing the messages.
                scoped.failed_conversations += 1
                scoped.errors.append(f"{conversation.get('conversation_id')}: {type(exc).__name__}")
                logger.exception("window scan failed for %s", conversation.get("conversation_id"))
        for (scope_type, scope_id), scoped in per_scope.items():
            merge_outcome(outcome, scoped)
            self._record_scan_run(scope_type, scope_id, scoped)
        if self.cache is not None:
            stats = self.cache.stats()
            if stats["hits"]:
                # Only worth a line when it actually saved calls.
                logger.info(
                    "window scan reused %s extraction(s): %s",
                    stats["hits"], stats,
                )
        return outcome

    def _extract(self, prompt: str) -> dict[str, Any]:
        """One model call: cached when repeated, spaced when rate limited."""
        if self.cache is not None:
            cached = self.cache.get(prompt)
            if cached is not None:
                return cached
        self.rate_limiter.wait()
        payload = self.extractor.extract(prompt)
        # Only successful calls reach here; a raise must never be cached as if
        # the model had answered.
        if self.cache is not None:
            self.cache.put(prompt, payload)
        return payload

    def _record_scan_run(
        self, scope_type: str, scope_id: str, scoped: WindowScanOutcome
    ) -> None:
        """Persist the sweep so the empty-window rate is visible in production.

        Observability must never break the scan itself, so a failure here is
        logged and dropped.
        """
        recorder = getattr(self.repository, "record_scan_run", None)
        if not callable(recorder):
            return
        try:
            recorder(
                scope_type=scope_type,
                scope_id=scope_id,
                conversations=scoped.conversations,
                windows=scoped.windows,
                empty_windows=scoped.empty_windows,
                mounts=scoped.mounts,
                candidates=scoped.candidates,
                relations=scoped.relations,
                failed_conversations=scoped.failed_conversations,
            )
        except Exception:
            logger.warning("scan run was not recorded", exc_info=True)

    def _scan_conversation(self, conversation: dict[str, Any], outcome: WindowScanOutcome) -> None:
        scope_type = conversation["scope_type"]
        scope_id = conversation["scope_id"]
        conversation_id = conversation["conversation_id"]
        entities, aliases, _ = self.repository.load_entity_registry(
            scope_type=scope_type, scope_id=scope_id
        )
        matcher = EntityMatcher(
            entities, aliases, registry_version=max((e.registry_version for e in entities), default=1)
        )
        watermark = self.repository.get_scan_watermark(
            scope_type=scope_type, scope_id=scope_id, conversation_id=conversation_id
        )
        chunks = self.repository.list_conversation_chunks(
            scope_type=scope_type,
            scope_id=scope_id,
            conversation_id=conversation_id,
            after_sent_at=watermark,
            limit=self.max_chunks_per_conversation,
        )
        if not chunks:
            return
        context: list[Chunk] = []
        if watermark:
            context_loader = getattr(self.repository, "list_conversation_context", None)
            if callable(context_loader):
                context = context_loader(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    conversation_id=conversation_id,
                    before_sent_at=watermark,
                    limit=max(0, self.window_size - 1),
                )
        prompt_chunks = _dedupe_chunks([*context, *chunks])
        new_chunk_ids = {chunk.chunk_id for chunk in chunks}
        windows = [
            window
            for window in build_windows(prompt_chunks, self.window_size, self.window_step)
            if any(chunk.chunk_id in new_chunk_ids for chunk in window)
        ]
        results = self._run_windows(
            scope_type=scope_type,
            scope_id=scope_id,
            windows=windows,
            matcher=matcher,
            registry_version=max((e.registry_version for e in entities), default=1),
        )
        for result in results:
            outcome.windows += 1
            if not result.entities:
                outcome.empty_windows += 1
            outcome.mounts += result.mounts
            outcome.candidates += result.candidates
            outcome.relations += result.relations
        if self.indexer is not None:
            changed_ids = sorted({
                chunk.chunk_id
                for window in windows
                for chunk in window
            })
            changed = self.repository.list_chunks(
                scope_type=scope_type,
                scope_id=scope_id,
                chunk_ids=changed_ids,
            ) if changed_ids else []
            update = getattr(self.indexer, "update_chunk_mounts", None)
            if callable(update):
                update(changed)
        last = chunks[-1]
        self.repository.set_scan_watermark(
            scope_type=scope_type,
            scope_id=scope_id,
            conversation_id=conversation_id,
            last_sent_at=str(last.sent_at),
            last_chunk_id=last.chunk_id,
            window_count=len(windows),
        )

    def _run_windows(
        self,
        *,
        scope_type: str,
        scope_id: str,
        windows: Sequence[Sequence[Chunk]],
        matcher: EntityMatcher,
        registry_version: int,
    ) -> list[WindowResult]:
        """Apply every window, or raise before the watermark moves.

        A partial conversation must not advance the watermark, so the first
        failed window aborts the batch even when others succeeded.
        """
        def apply(window: Sequence[Chunk]) -> WindowResult:
            return self._apply_window(
                scope_type=scope_type,
                scope_id=scope_id,
                window=window,
                matcher=matcher,
                registry_version=registry_version,
            )

        if self.concurrency > 1 and len(windows) > 1:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(windows))) as pool:
                futures = [pool.submit(apply, window) for window in windows]
                return [future.result() for future in futures]
        return [apply(window) for window in windows]

    def _apply_window(
        self,
        *,
        scope_type: str,
        scope_id: str,
        window: Sequence[Chunk],
        matcher: EntityMatcher,
        registry_version: int,
    ) -> WindowResult:
        result = WindowResult()
        prompt = build_extraction_prompt(window)
        payload = self._extract(prompt)
        extracted = clean_entities(payload.get("entities"))
        result.entities = len(extracted)
        relations = clean_relations(payload.get("relations"))

        # Resolve the model's names against the registry once per window. The
        # relation endpoints are included: the model sometimes lists an edge
        # between two known entities without repeating them in `entities`.
        lookup_keys = [normalized_text(item["name"]) for item in extracted]
        lookup_keys.extend(normalized_text(item["source"]) for item in relations)
        lookup_keys.extend(normalized_text(item["target"]) for item in relations)
        lookup = self.repository.find_entities_by_normalized(
            scope_type=scope_type,
            scope_id=scope_id,
            normalized_keys=lookup_keys,
        )

        # 1) Explicit mounts come from the chunk's own text and are the most
        #    trustworthy signal, so they are written even with no model output.
        for chunk in window:
            mounts = match_chunk_mounts(chunk, matcher)
            if mounts:
                self.repository.merge_chunk_mounts(chunk, mounts)
                result.mounts += len(mounts)

        # 2) Window-level mounts: the window is about this entity, so every
        #    message in it becomes reachable from that entity node.
        for item in extracted:
            resolved = lookup.get(normalized_text(item["name"]))
            if resolved is None:
                self._create_candidate(
                    scope_type=scope_type, scope_id=scope_id, item=item, window=window, result=result
                )
                continue
            tier = "window_strong" if item["confidence"] >= 0.8 else "window_weak"
            mounts = [
                EntityMount(
                    entity_id=resolved["entity_id"],
                    domain=resolved["domain"],
                    registry_version=resolved["registry_version"],
                    mount_method="window_batch",
                    confidence=MOUNT_CONFIDENCE[tier],
                )
            ]
            for chunk in window:
                self.repository.merge_chunk_mounts(chunk, mounts)
            result.mounts += len(mounts) * len(window)

        # 3) Relations need both endpoints to be real entities; a name the
        #    registry does not know cannot anchor an edge.
        evidence = [chunk.chunk_id for chunk in window]
        for relation in relations:
            source = lookup.get(normalized_text(relation["source"]))
            target = lookup.get(normalized_text(relation["target"]))
            if source is None or target is None:
                continue
            self.repository.upsert_entity_relation(
                scope_type=scope_type,
                scope_id=scope_id,
                source_entity_id=source["entity_id"],
                target_entity_id=target["entity_id"],
                relation_type=relation["type"],
                confidence=relation["confidence"],
                evidence_chunk_ids=evidence,
            )
            result.relations += 1
        return result

    def _create_candidate(
        self,
        *,
        scope_type: str,
        scope_id: str,
        item: dict[str, Any],
        window: Sequence[Chunk],
        result: WindowResult,
    ) -> None:
        anchor = next((chunk for chunk in window if item["name"] in (chunk.content or "")), window[0])
        excerpt = _excerpt(anchor.content, item["name"])
        self.repository.upsert_candidate_mention(
            scope_type=scope_type,
            scope_id=scope_id,
            candidate_name=item["name"],
            normalized_key=normalized_text(item["name"]),
            domain=item["domain"],
            chunk=anchor,
            context_excerpt=excerpt,
            confidence=item["confidence"],
            method="llm",
        )
        result.candidates += 1


def merge_outcome(target: WindowScanOutcome, source: WindowScanOutcome) -> None:
    """Add one scope's counters into the sweep total."""
    target.conversations += source.conversations
    target.windows += source.windows
    target.empty_windows += source.empty_windows
    target.mounts += source.mounts
    target.candidates += source.candidates
    target.relations += source.relations
    target.failed_conversations += source.failed_conversations
    target.errors.extend(source.errors)


def build_windows(chunks: Sequence[Chunk], size: int, step: int) -> list[list[Chunk]]:
    """Overlapping windows that always cover the tail.

    A plain range() would drop the last messages whenever the count is not a
    multiple of the step, and those are exactly the ones a fresh scan cares
    about.
    """
    if not chunks:
        return []
    if len(chunks) <= size:
        return [list(chunks)]
    windows: list[list[Chunk]] = []
    start = 0
    while start < len(chunks):
        windows.append(list(chunks[start:start + size]))
        if start + size >= len(chunks):
            break
        start += step
    return windows


def _dedupe_chunks(chunks: Iterable[Chunk]) -> list[Chunk]:
    seen: set[str] = set()
    output: list[Chunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        output.append(chunk)
    return output


def build_extraction_prompt(
    window: Sequence[Chunk],
) -> str:
    """Render one window for the extraction model.

    The scope's known entities are deliberately absent. Measured on real
    windows: a list of ~10+ entries made the model return nothing at all
    (4 runs: 3,0,0,0 entities), and even after rewording it left 43.75% of
    windows empty with a third of them unstable between runs. Without the list
    the same windows returned entities 24/24 times. Nothing downstream used the
    ids the model would have echoed anyway - entity resolution happens in code
    via name lookup - so the list was pure cost.
    """
    lines: list[str] = []
    for chunk in window:
        sender = str(chunk.context_header.get("sender_display_name") or "未知")
        content = " ".join((chunk.content or "").split())
        lines.append(f"[{sender}] {content[:300]}")
    return _WINDOW_PROMPT.format(window="\n".join(lines))


def clean_entities(raw: Any) -> list[dict[str, Any]]:
    """Keep only entities this release can store.

    A wrong type is worse than a missing entity: it puts the node in the wrong
    place in the tree, and type is used as a ranking signal downstream.
    """
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        domain = str(item.get("type") or "").strip()
        if not name or domain not in ALLOWED_DOMAINS:
            continue
        evidence = str(item.get("evidence") or "")
        if _looks_like_credential(name) or _looks_like_credential(evidence):
            continue
        if _GENERIC_SUBJECT_PATTERN.match(name):
            continue
        key = normalized_text(name)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append({
            "name": name,
            "domain": domain,
            "confidence": _clamp(item.get("confidence"), 0.0, 1.0),
            "mentions": [str(value) for value in (item.get("mentions") or []) if str(value).strip()][:10],
            "evidence": evidence,
        })
    return output


def clean_relations(raw: Any) -> list[dict[str, Any]]:
    """Drop relations outside the enum.

    Measured behaviour: with the enum only shown in the output example the model
    returned "discusses"; an out-of-enum edge is useless to graph traversal and
    would have to be migrated away later.
    """
    output: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        relation_type = str(item.get("type") or "").strip()
        if relation_type not in ALLOWED_RELATIONS:
            continue
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target or normalized_text(source) == normalized_text(target):
            continue
        output.append({
            "source": source,
            "target": target,
            "type": relation_type,
            "confidence": _clamp(item.get("confidence"), 0.0, 1.0),
        })
    return output


def _looks_like_credential(value: str) -> bool:
    text = str(value or "")
    return bool(_CREDENTIAL_PATTERN.search(text) or _LONG_DIGITS.search(text))


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _excerpt(content: str, name: str, radius: int = 80) -> str:
    text = str(content or "")
    index = text.find(name)
    if index < 0:
        return text[:240]
    start = max(0, index - radius)
    return text[start:index + len(name) + 120]
