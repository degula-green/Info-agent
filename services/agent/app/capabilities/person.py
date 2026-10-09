"""person.query: answer a question about one named person.

The capability resolves a name to a person, then answers from two possible
paths. When the Knowledge service already has a generated profile or extracted
facts, that cache is the fast path. Otherwise the capability exports the whole
visible message scope (sent / private / mention), batch-extracts attributed
facts, and lets the answer model speak from those facts. It never relies on a
single weak retrieval query to surface a phone number or a student id.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.capabilities.knowledge import (
    _answer_evidence,
    _known_knowledge_citations,
    _merge_content,
    _parallel_scope_calls,
    _scope_requests,
)
from app.capabilities.person_profile import (
    CATEGORY_LABELS,
    FACT_TYPE_TO_CATEGORY,
    QuestionMode,
    coverage_summary,
    fact_evidence,
    facts_by_person_section,
    merge_facts,
    missing_fact_values,
    normalize_value,
    profile_sections,
    question_mode,
    render_person_sections,
    snapshot_fingerprint,
)
from app.capabilities.person_scope import (
    PersonScopeUnavailable,
    collect_resource_ids,
    export_person_scope,
    identity_ids_of,
    mention_anchors,
    private_conversation_ids_of,
    relation_ids_of,
    resolve_person,
    search_content,
)
from app.infrastructure.knowledge.client import (
    KnowledgeClient,
    KnowledgeUnavailable,
)
from app.infrastructure.rag.client import RAGClient
from app.kernel.execution_context import current_execution_context
from app.kernel.models import CapabilityDescriptor
from app.providers.person_facts import PersonFact, PersonFactProvider

PERSON_QUERY_NAME = "person.query"

logger = logging.getLogger("agent.person")

NO_PERSON = "我这边没有关于这个人的资料。"
NO_MESSAGES = "没有找到与他相关的可见消息。"
NO_CONTENT = "没有找到能回答这个问题的内容。"

DEFAULT_EXTRACTION_BATCH_CHARS = 6000
DEFAULT_EXTRACTION_BATCH_ITEMS = 40
DEFAULT_EXTRACTION_WORKERS = 3
DEFAULT_SELF_PRIVATE_DAYS = 20


class PersonQueryUnavailable(RuntimeError):
    pass


class PersonQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=2000)
    time_range: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    # Empty means all three sources. Values: sent / private / mention.
    source_scope: list[str] = Field(default_factory=list, max_length=3)
    top_k: int = Field(default=10, ge=1, le=50)


class PersonQueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[dict[str, Any]]
    subject_name: str = ""
    person_key: str = ""
    matched_count: int = 0
    needs_disambiguation: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: int = 0
    evidence_count: int = 0
    coverage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PersonQueryCapability:
    descriptor = CapabilityDescriptor(
        name=PERSON_QUERY_NAME,
        description=(
            "回答关于某个具体人物的问题（只读）：先定位这个人，"
            "再在“他发的 / 我和他的私聊 / 提到他的”完整范围内抽取事实并给出带来源的回答。"
        ),
        input_schema=PersonQueryInput.model_json_schema(),
        output_schema=PersonQueryOutput.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=60,
    )

    def __init__(
        self,
        rag_client: RAGClient,
        knowledge_client: KnowledgeClient,
        provider,
        *,
        fact_extractor: PersonFactProvider | None = None,
        fact_store: Any | None = None,
        timeout_seconds: int | None = None,
        scope_page_size: int = 50,
        max_scope_chunks: int = 5000,
        extraction_batch_chars: int = DEFAULT_EXTRACTION_BATCH_CHARS,
        extraction_batch_items: int = DEFAULT_EXTRACTION_BATCH_ITEMS,
    ) -> None:
        self.rag = rag_client
        self.knowledge = knowledge_client
        self.provider = provider
        self.fact_extractor = fact_extractor
        self.fact_store = fact_store
        self.scope_page_size = max(1, min(int(scope_page_size), 50))
        self.max_scope_chunks = max(1, int(max_scope_chunks))
        self.extraction_batch_chars = max(1000, int(extraction_batch_chars))
        self.extraction_batch_items = max(1, int(extraction_batch_items))
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> PersonQueryInput:
        return PersonQueryInput.model_validate(arguments)

    def execute(self, arguments: PersonQueryInput) -> dict[str, Any]:
        context = current_execution_context()
        try:
            subject, matches = resolve_person(
                self.knowledge,
                owner_user_id=context.owner_user_id,
                name=arguments.name,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        except PersonScopeUnavailable as exc:
            raise PersonQueryUnavailable("person lookup is unavailable") from exc
        if not matches:
            return self._fallback(context, arguments, subject)
        if len(matches) > 1:
            candidates = [
                {
                    "person_key": item.get("person_key"),
                    "display_name": item.get("display_name"),
                }
                for item in matches
            ]
            names = "、".join(str(item.get("display_name") or subject) for item in matches)
            return PersonQueryOutput(
                answer=f"我找到了多个同名的人，请确认是哪一位：{names}。",
                citations=[],
                subject_name=subject,
                matched_count=len(matches),
                needs_disambiguation=True,
                candidates=candidates,
            ).model_dump()

        match = matches[0]
        display_name = str(match.get("display_name") or "")
        aliases = self._person_aliases(match, subject, display_name)
        identity_ids = identity_ids_of(match)
        private_ids = private_conversation_ids_of(match)
        person_key = str(match.get("person_key") or "")

        # Fast path: the Knowledge service already has a profile or facts for an
        # attached contact. This is a cache, not the source of truth.
        cache_evidence = self._cache_evidence(context, match)
        mode = question_mode(arguments.question)
        overview = mode.mode == "overview"
        selected_cache = (
            cache_evidence
            if overview
            else self._cache_evidence_for_facet(cache_evidence, mode.facet)
        )
        if selected_cache and self.fact_extractor is None:
            return self._answer(
                arguments,
                selected_cache,
                subject=subject,
                display_name=display_name,
                aliases=aliases,
                person_key=person_key,
                coverage={
                    "path": "cache",
                    "evidence_count": len(selected_cache),
                },
            )

        # Main path: export the whole person scope and extract facts in batches.
        if self.fact_extractor is not None:
            source_scope = list(arguments.source_scope)
            anchors = mention_anchors(match, fallback=subject)
            private_occurred_after = None
            if (
                person_key
                and person_key == context.owner_user_id
                and (not source_scope or "private" in source_scope)
            ):
                private_occurred_after = (
                    datetime.now(timezone.utc)
                    - timedelta(days=DEFAULT_SELF_PRIVATE_DAYS)
                ).isoformat().replace("+00:00", "Z")
            scope = export_person_scope(
                self.rag,
                owner_user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                identity_ids=identity_ids,
                private_ids=private_ids,
                subject=subject,
                source_scope=source_scope,
                mention_anchors=anchors,
                private_occurred_after=private_occurred_after,
                page_size=self.scope_page_size,
                max_chunks=self.max_scope_chunks,
            )
            if not scope.chunks:
                if selected_cache:
                    return self._answer(
                        arguments,
                        selected_cache,
                        subject=subject,
                        display_name=display_name,
                        aliases=aliases,
                        person_key=person_key,
                        coverage={
                            "path": "cache",
                            "evidence_count": len(selected_cache),
                        },
                    )
                return PersonQueryOutput(
                    answer=NO_MESSAGES,
                    citations=[],
                    subject_name=subject,
                    person_key=person_key,
                    matched_count=1,
                    coverage=scope.coverage,
                ).model_dump()
            # Always build one complete, question-independent fact snapshot.
            # Facet filtering happens only when evidence is sent to the answer
            # model; otherwise a facet query could poison the snapshot and make
            # a later overview reuse only that one category.
            extraction_question = (
                f"{arguments.question}\n\n"
                "请抽取输入消息中所有类别的事实，覆盖身份、联系方式、"
                "角色/单位、工作、项目、近期活动、关系/群组、偏好和其他"
                "明确事实。忽略问题里的范围限制，不要因为问题只问某个方面"
                "而漏掉其他明确事实。"
            )

            fingerprint = snapshot_fingerprint(scope.chunks)
            merged_facts = self._load_fact_snapshot(
                context,
                person_key,
                fingerprint,
            )
            snapshot_hit = merged_facts is not None
            extraction_calls = 0
            failed_batches = 0
            if merged_facts is None:
                facts, extraction_calls, failed_batches = self._extract_facts(
                    scope.chunks,
                    subject=subject,
                    display_name=display_name,
                    aliases=aliases,
                    question=extraction_question,
                )
                merged_facts = merge_facts(facts)
                self._save_fact_snapshot(
                    context,
                    person_key,
                    fingerprint,
                    merged_facts,
                )
            selected_facts = [
                item
                for item in merged_facts
                if not mode.facet or item.get("category") == mode.facet
            ]
            profile_evidence = self._merge_evidence(
                [*selected_cache, *fact_evidence(selected_facts)]
            )

            if mode.mode == "facet" and not profile_evidence:
                return PersonQueryOutput(
                    answer=(
                        f"没有拿到“{CATEGORY_LABELS.get(str(mode.facet or ''), str(mode.facet or '该方面'))}"
                        "”方面的信息。"
                    ),
                    citations=[],
                    subject_name=subject,
                    person_key=person_key,
                    matched_count=1,
                    coverage={
                        **scope.coverage,
                        "path": "facet_no_facts",
                        "mode": mode.mode,
                        "facet": mode.facet,
                        "snapshot": fingerprint[:16],
                        "snapshot_hit": snapshot_hit,
                    },
                ).model_dump()

            evidence = self._merge_evidence(profile_evidence)
            related_evidence = self._chunk_evidence(
                scope.chunks,
                limit=12,
                related=True,
            )
            warnings: list[str] = []
            if scope.truncated:
                warnings.append("person_scope_truncated")
            if failed_batches:
                warnings.append(f"fact_extraction_failed_batches={failed_batches}")
            if (
                mode.mode == "overview"
                and len(profile_evidence) < 3
                and related_evidence
            ):
                # Sparse extraction must not reduce an overview to one line.
                # Keep the extracted facts, and add a bounded set of visible
                # conversation excerpts so the answer can describe what is
                # actually available about the person.
                evidence = self._merge_evidence([*evidence, *related_evidence])
                warnings.append("sparse_facts_related_content_added")
            if not evidence:
                evidence = related_evidence or self._chunk_evidence(scope.chunks)
                warnings.append("fact_extraction_empty")
            if not evidence:
                return PersonQueryOutput(
                    answer=NO_CONTENT,
                    citations=[],
                    subject_name=subject,
                    person_key=person_key,
                    matched_count=1,
                    coverage=scope.coverage,
                    warnings=warnings,
                ).model_dump()
            return self._answer(
                arguments,
                evidence,
                subject=subject,
                display_name=display_name,
                aliases=aliases,
                person_key=person_key,
                mode=mode,
                facts=selected_facts,
                coverage={
                    **scope.coverage,
                    "path": (
                        "cache+full_scope_extraction"
                        if selected_cache and profile_evidence
                        else "full_scope_extraction"
                    ),
                    "chunks": len(scope.chunks),
                    "facts": len(merged_facts),
                    "selected_facts": len(selected_facts),
                    "cache_evidence": len(selected_cache),
                    "mention_anchors": list(anchors),
                    "private_occurred_after": private_occurred_after,
                    "related_evidence": len(related_evidence),
                    "snapshot": fingerprint[:16],
                    "snapshot_hit": snapshot_hit,
                    "mode": mode.mode,
                    "facet": mode.facet,
                },
                warnings=warnings,
                extra_model_calls=extraction_calls,
            )

        return self._legacy_path(
            context,
            arguments,
            subject=subject,
            display_name=display_name,
            aliases=aliases,
            person_key=person_key,
            identity_ids=identity_ids,
            private_ids=private_ids,
        )

    # -- cache ---------------------------------------------------------------

    def _load_fact_snapshot(
        self,
        context,
        person_key: str,
        fingerprint: str,
    ) -> list[dict[str, Any]] | None:
        getter = getattr(self.fact_store, "get_person_fact_snapshot", None)
        if not callable(getter):
            return None
        try:
            value = getter(
                owner_user_id=context.owner_user_id,
                person_key=person_key,
                snapshot_fingerprint=fingerprint,
            )
        except Exception:
            logger.exception("person fact snapshot read failed")
            return None
        if value is None:
            return None
        return [item for item in value if isinstance(item, dict)]

    def _save_fact_snapshot(
        self,
        context,
        person_key: str,
        fingerprint: str,
        facts: list[dict[str, Any]],
    ) -> None:
        saver = getattr(self.fact_store, "save_person_fact_snapshot", None)
        if not callable(saver):
            return
        try:
            saver(
                owner_user_id=context.owner_user_id,
                person_key=person_key,
                snapshot_fingerprint=fingerprint,
                facts=facts,
            )
        except Exception:
            logger.exception("person fact snapshot write failed")

    def _cache_evidence(
        self,
        context,
        match: dict[str, Any],
    ) -> list[dict[str, Any]]:
        relation_ids = relation_ids_of(match)
        getter = getattr(self.knowledge, "person_profile", None)
        if not relation_ids or not callable(getter):
            return []
        try:
            payload = getter(
                owner_user_id=context.owner_user_id,
                relation_ids=relation_ids,
                organization_id=context.organization_id or "",
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        except KnowledgeUnavailable:
            return []
        if not isinstance(payload, dict):
            return []
        evidence: list[dict[str, Any]] = []
        profile = payload.get("profile") or {}
        summary = str(profile.get("summary") or "").strip()
        if summary:
            evidence.append(
                {
                    "evidence_id": "person-profile",
                    "fetch_method": "person_cache",
                    "attribution": "profile",
                    "fact_type": "profile",
                    "fact_category": "identity",
                    "fact_label": "人物画像",
                    "fact_value": summary,
                    "quote": summary,
                    "snippet": summary,
                    "person_name": payload.get("display_name") or "",
                }
            )
        for raw in payload.get("facts") or []:
            if not isinstance(raw, dict):
                continue
            value = str(raw.get("raw_value") or "").strip()
            if not value:
                continue
            label = str(raw.get("label") or raw.get("fact_type") or "").strip()
            quote = f"{label}: {value}" if label else value
            evidence.append(
                {
                    "evidence_id": str(raw.get("id") or f"fact:{value}"),
                    "fetch_method": "person_cache",
                    "attribution": "subject_said",
                    "fact_type": str(raw.get("fact_type") or ""),
                    "fact_label": label,
                    "fact_value": value,
                    "quote": quote,
                    "snippet": quote,
                    "sent_at": raw.get("occurred_at"),
                    "person_name": payload.get("display_name") or "",
                }
            )
        return evidence

    @staticmethod
    def _cache_evidence_for_facet(
        evidence: list[dict[str, Any]],
        facet: str | None,
    ) -> list[dict[str, Any]]:
        if not facet:
            return []
        return [
            item
            for item in evidence
            if FACT_TYPE_TO_CATEGORY.get(str(item.get("fact_type") or ""))
            == facet
        ]

    @staticmethod
    def _is_overview_question(question: str) -> bool:
        normalized = " ".join(str(question or "").split())
        return any(
            marker in normalized
            for marker in (
                "情况",
                "介绍",
                "了解",
                "基本信息",
                "项目",
                "工作",
                "职责",
                "负责",
                "任务",
                "进展",
                "进度",
                "最近",
                "近期",
                "做什么",
                "忙什么",
            )
        )

    @staticmethod
    def _person_aliases(
        match: dict[str, Any],
        subject: str,
        display_name: str,
    ) -> list[str]:
        """All names that denote the resolved person.

        The contact remark and the platform display name are often different
        (for example 杨思琪 / Andrea and 小超 / 德古拉green). Every downstream
        model must know that both names refer to the same subject.
        """

        values: list[str] = [subject, display_name]
        for identity in match.get("identities") or []:
            if not isinstance(identity, dict):
                continue
            values.extend(
                [
                    str(identity.get("display_name") or ""),
                    str(identity.get("external_user_id") or ""),
                ]
            )
        aliases: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = " ".join(str(value or "").split())
            if not name or name in seen:
                continue
            seen.add(name)
            aliases.append(name)
        return aliases

    @classmethod
    def _merge_evidence(
        cls,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge cache facts with freshly extracted facts without duplicates."""

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            fact_type = str(item.get("fact_type") or item.get("fetch_method") or "")
            value = cls._normalize_text(
                str(item.get("fact_value") or item.get("quote") or "")
            )
            key = (fact_type, value or str(item.get("evidence_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    # -- full-scope extraction ----------------------------------------------

    def _extract_facts(
        self,
        chunks: list[dict[str, Any]],
        *,
        subject: str,
        display_name: str,
        question: str,
        aliases: list[str] | None = None,
    ) -> tuple[list[PersonFact], int, int]:
        extractor = self.fact_extractor
        if extractor is None:
            return [], 0, 0
        messages = self._messages_from_chunks(chunks)
        batches = self._batches(messages)
        if not batches:
            return [], 0, 0
        alias_values = list(aliases or [])
        if not alias_values:
            alias_values = [value for value in {subject, display_name} if value]

        def run(batch: list[dict[str, Any]]) -> tuple[list[PersonFact], bool]:
            try:
                facts = list(
                    extractor.extract(
                        subject=display_name or subject,
                        aliases=alias_values,
                        question=question,
                        messages=batch,
                    )
                )
            except Exception:
                # One malformed model batch must not discard the whole archive.
                logger.exception("person fact extraction failed for one batch")
                return [], True
            grounded: list[PersonFact] = []
            for fact in facts:
                item = self._ground_fact(fact, batch, aliases)
                if item is not None:
                    grounded.append(item)
            return grounded, False

        merged: dict[tuple[str, str], PersonFact] = {}
        workers = max(1, min(DEFAULT_EXTRACTION_WORKERS, len(batches)))
        failed_batches = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for facts, failed in pool.map(run, batches):
                if failed:
                    failed_batches += 1
                for fact in facts:
                    key = (fact.fact_type, " ".join(fact.value.lower().split()))
                    current = merged.get(key)
                    if current is None or self._fact_rank(fact) > self._fact_rank(current):
                        merged[key] = fact
        ordered = sorted(
            merged.values(),
            key=lambda fact: (fact.speaker_role != "subject", fact.fact_type, fact.value),
        )
        return ordered, len(batches), failed_batches

    @staticmethod
    def _normalize_text(value: str) -> str:
        return "".join(
            character
            for character in str(value or "").lower()
            if not character.isspace()
            and character not in "，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》-—_·"
        )

    @classmethod
    def _ground_fact(
        cls,
        fact: PersonFact,
        batch: list[dict[str, Any]],
        aliases: list[str],
    ) -> PersonFact | None:
        """Ground one model fact against the real messages in its batch.

        The extraction model proposes the fact; this layer decides whether it
        is supported and who actually said it. The speaker role always comes
        from the message sender, never from the model, and an ``other`` fact
        must mention the subject by name (otherwise it is a fact about the
        other person, not about the subject).
        """

        messages = [message for message in batch if isinstance(message, dict)]
        if not messages:
            return None
        source = cls._normalize_text(
            "\n".join(str(message.get("text") or "") for message in messages)
        )
        value = cls._normalize_text(fact.value)
        quote = cls._normalize_text(fact.quote)
        if not source or not value or value not in source:
            return None
        if quote and (quote not in source or value not in quote):
            return None
        message = next(
            (
                item
                for item in messages
                if str(item.get("resource_id") or "") == fact.resource_id
            ),
            None,
        )
        if message is None:
            message = next(
                (
                    item
                    for item in messages
                    if quote
                    and quote
                    in cls._normalize_text(str(item.get("text") or ""))
                ),
                None,
            )
        if message is None:
            return None
        message_text = cls._normalize_text(str(message.get("text") or ""))
        actual_role = str(message.get("speaker_role") or "other")
        if actual_role != "subject":
            alias_names = {
                cls._normalize_text(alias)
                for alias in aliases
                if cls._normalize_text(alias)
            }
            if not any(alias in message_text or alias in quote for alias in alias_names):
                return None
        return fact.model_copy(
            update={
                "speaker_role": actual_role,
                "resource_id": str(message.get("resource_id") or fact.resource_id),
                "sent_at": fact.sent_at
                or str(message.get("sent_at") or ""),
            }
        )

    @staticmethod
    def _fact_rank(fact: PersonFact) -> tuple[int, int, str]:
        return (
            1 if fact.speaker_role == "subject" else 0,
            len(fact.quote),
            fact.sent_at,
        )

    def _messages_from_chunks(
        self,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for item in chunks:
            resource_id = str(item.get("resource_id") or "")
            if not resource_id:
                continue
            message = grouped.setdefault(
                resource_id,
                {
                    "resource_id": resource_id,
                    "resource_type": item.get("resource_type"),
                    "sender_name": (item.get("sender") or {}).get("name"),
                    "sender_platform": (item.get("sender") or {}).get("platform"),
                    "conversation_name": (item.get("conversation") or {}).get("name"),
                    "conversation_type": (item.get("conversation") or {}).get("type"),
                    "sent_at": item.get("sent_at"),
                    "speaker_role": item.get("speaker_role"),
                    "source_group": item.get("source_group"),
                    "attribution": item.get("attribution"),
                    "parts": [],
                },
            )
            text = str(item.get("text") or "").strip()
            if text and not self._is_raw_payload(text):
                message["parts"].append(text)
        messages: list[dict[str, Any]] = []
        for message in grouped.values():
            text = "\n".join(message.pop("parts"))
            if not text:
                continue
            message["text"] = text
            messages.append(message)
        messages.sort(key=lambda item: str(item.get("sent_at") or ""))
        return messages

    @staticmethod
    def _is_raw_payload(text: str) -> bool:
        """WeChat emoji/system payloads are transport data, not person facts."""

        normalized = str(text or "").lstrip().lower()
        return (
            normalized.startswith("<msg")
            or "<appmsg" in normalized
            or "<emoji" in normalized
            or "<sysmsg" in normalized
        )

    def _batches(
        self,
        messages: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        size = 0
        for message in messages:
            length = len(str(message.get("text") or ""))
            if current and (
                size + length > self.extraction_batch_chars
                or len(current) >= self.extraction_batch_items
            ):
                batches.append(current)
                current = []
                size = 0
            current.append(message)
            size += length
        if current:
            batches.append(current)
        return batches

    def _fact_evidence(self, facts: Iterable[PersonFact]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for fact in facts:
            digest = hashlib.sha256(
                f"{fact.fact_type}|{fact.value}|{fact.resource_id}".encode("utf-8")
            ).hexdigest()[:20]
            quote = str(fact.quote or fact.value)
            evidence.append(
                {
                    "evidence_id": f"person-fact:{digest}",
                    "fetch_method": "person_fact",
                    "attribution": (
                        "subject_said"
                        if fact.speaker_role == "subject"
                        else "other_said"
                    ),
                    "fact_type": fact.fact_type,
                    "fact_label": fact.label,
                    "fact_value": fact.value,
                    "quote": quote,
                    "snippet": quote,
                    "resource_id": fact.resource_id,
                    "sent_at": fact.sent_at or None,
                }
            )
        return evidence

    def _chunk_evidence(
        self,
        chunks: list[dict[str, Any]],
        *,
        limit: int = 12,
        related: bool = False,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in chunks:
            text = str(item.get("text") or "").strip()
            if not text or self._is_raw_payload(text):
                continue
            normalized = self._normalize_text(text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            evidence.append(
                {
                    "evidence_id": str(item.get("chunk_id") or ""),
                    "fetch_method": "knowledge",
                    "evidence_kind": (
                        "conversation_excerpt" if related else "chunk"
                    ),
                    "attribution": item.get("attribution") or "other_said",
                    "quote": text,
                    "snippet": text,
                    "resource_id": item.get("resource_id"),
                    "sender_name": (item.get("sender") or {}).get("name"),
                    "conversation_name": (item.get("conversation") or {}).get("name"),
                    "conversation_type": (item.get("conversation") or {}).get("type"),
                    "conversation_platform": (item.get("conversation") or {}).get(
                        "platform"
                    ),
                    "sent_at": item.get("sent_at"),
                }
            )
            if len(evidence) >= max(1, int(limit)):
                break
        return evidence

    # -- legacy fallback -----------------------------------------------------

    def _legacy_path(
        self,
        context,
        arguments: PersonQueryInput,
        *,
        subject: str,
        display_name: str,
        aliases: list[str] | None = None,
        person_key: str,
        identity_ids: list[str],
        private_ids: list[str],
    ) -> dict[str, Any]:
        resource_ids = collect_resource_ids(
            self.rag,
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            identity_ids=identity_ids,
            private_ids=private_ids,
            subject=subject,
            source_scope=arguments.source_scope,
            top_k=arguments.top_k,
        )
        if not resource_ids:
            return PersonQueryOutput(
                answer=NO_MESSAGES,
                citations=[],
                subject_name=subject,
                person_key=person_key,
                matched_count=1,
            ).model_dump()
        results = search_content(
            self.rag,
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            resource_ids=resource_ids,
            question=arguments.question,
            name=arguments.name,
            subject=subject,
            top_k=arguments.top_k,
        )
        evidence = _answer_evidence(results, arguments.timezone)
        if not evidence:
            return PersonQueryOutput(
                answer=NO_CONTENT,
                citations=[],
                subject_name=subject,
                person_key=person_key,
                matched_count=1,
            ).model_dump()
        return self._answer(
            arguments,
            evidence,
            subject=subject,
            display_name=display_name,
            aliases=aliases,
            person_key=person_key,
            coverage={"path": "legacy_retrieval", "resources": len(resource_ids)},
        )

    def _fallback(self, context, arguments: PersonQueryInput, subject: str) -> dict[str, Any]:
        """Generic retrieval when no person matches, so the turn is not lost."""

        scopes = _scope_requests(
            owner_user_id=context.owner_user_id,
            organization_id=context.organization_id,
            include_personal=True,
        )
        body = {
            "query": arguments.question,
            "resource_ids": [],
            "top_k": arguments.top_k,
            "include_protected": True,
            "group_by_source": True,
        }
        responses = _parallel_scope_calls(
            scopes,
            lambda scope: self.rag.search_content(
                {**body, **scope},
                user_id=context.owner_user_id,
                organization_id=context.organization_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )
        results = _merge_content(responses, limit=arguments.top_k, min_score=0.0)
        evidence = _answer_evidence(results, arguments.timezone)
        if not evidence:
            return PersonQueryOutput(
                answer=NO_PERSON, citations=[], subject_name=subject
            ).model_dump()
        return self._answer(
            arguments,
            evidence,
            subject=subject,
            coverage={"path": "generic_fallback", "evidence_count": len(evidence)},
        )

    # -- answer --------------------------------------------------------------

    def _answer(
        self,
        arguments: PersonQueryInput,
        evidence: list[dict[str, Any]],
        *,
        subject: str = "",
        display_name: str = "",
        aliases: list[str] | None = None,
        mode: QuestionMode | None = None,
        facts: list[dict[str, Any]] | None = None,
        person_key: str = "",
        coverage: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        extra_model_calls: int = 0,
    ) -> dict[str, Any]:
        answer = ""
        citations: list[dict[str, Any]] = []
        model_calls = max(int(extra_model_calls), 0)
        sectioned = False
        question = self._question_with_context(
            arguments,
            subject,
            display_name,
            aliases=aliases,
            mode=mode,
            facts=facts,
        )
        fact_sections = facts_by_person_section(
            self._section_facts(facts or [], evidence),
            facet=mode.facet if mode is not None and mode.mode == "facet" else None,
        )
        conversation_excerpts = [
            item
            for item in evidence
            if isinstance(item, dict)
            and item.get("evidence_kind") == "conversation_excerpt"
        ]
        if fact_sections or conversation_excerpts:
            summaries: dict[str, str] = {}
            composer = getattr(self.provider, "compose_person_sections", None)
            if callable(composer):
                try:
                    summaries = composer(
                        question,
                        fact_sections,
                        conversation_excerpts=conversation_excerpts,
                        time_range=arguments.time_range,
                    )
                    model_calls += max(
                        int(getattr(self.provider, "last_call_count", 0) or 0),
                        0,
                    )
                except Exception:
                    logger.exception(
                        "person section composition failed; using deterministic sections"
                    )
            answer = render_person_sections(
                fact_sections,
                summaries=summaries,
                timezone_name=arguments.timezone,
            )
            if answer:
                citations = self._evidence_citations(evidence)
                sectioned = True
        if not sectioned:
            draft = self._compose(
                arguments,
                evidence,
                subject,
                display_name,
                aliases=aliases,
                mode=mode,
                facts=facts,
            )
            answer = str(draft.answer)
            missing = missing_fact_values(answer, facts or [])
            if missing:
                lines = ["补充信息："]
                seen_values: set[str] = set()
                for item in missing:
                    value = str(item.get("value") or "").strip()
                    if not value or value in seen_values:
                        continue
                    seen_values.add(value)
                    label = CATEGORY_LABELS.get(
                        str(item.get("category") or ""),
                        str(item.get("category") or "其他"),
                    )
                    lines.append(f"- {label}：{value}")
                if len(lines) > 1:
                    answer = answer.rstrip() + "\n\n" + "\n".join(lines)
            citations = _known_knowledge_citations(draft.citations, evidence)
            model_calls += max(int(getattr(draft, "model_calls", 0) or 0), 0)
        return PersonQueryOutput(
            answer=answer,
            citations=citations,
            subject_name=subject,
            person_key=person_key,
            matched_count=1,
            model_calls=model_calls,
            evidence_count=len(evidence),
            coverage={
                **dict(coverage or {}),
                "coverage": coverage_summary(
                    facts or [],
                    mode=mode or QuestionMode(mode="overview"),
                ),
            },
            warnings=list(warnings or []),
        ).model_dump()

    def _compose(
        self,
        arguments: PersonQueryInput,
        evidence: list[dict[str, Any]],
        subject: str = "",
        display_name: str = "",
        *,
        aliases: list[str] | None = None,
        mode: QuestionMode | None = None,
        facts: list[dict[str, Any]] | None = None,
    ):
        question = self._question_with_context(
            arguments,
            subject,
            display_name,
            aliases=aliases,
            mode=mode,
            facts=facts,
        )
        if arguments.time_range:
            return self.provider.compose(
                question, evidence, time_range=arguments.time_range
            )
        return self.provider.compose(question, evidence)

    @staticmethod
    def _question_with_context(
        arguments: PersonQueryInput,
        subject: str = "",
        display_name: str = "",
        *,
        aliases: list[str] | None = None,
        mode: QuestionMode | None = None,
        facts: list[dict[str, Any]] | None = None,
    ) -> str:
        question = arguments.question
        other_aliases = [
            value
            for value in (aliases or [])
            if value and value != subject and value != display_name
        ]
        if subject and other_aliases:
            # Tell the answer model that the name the user typed and the name
            # shown in the evidence are the same person; otherwise it reads
            # the alias as an unrelated string and denies having information.
            question = (
                f"{question}\n\n"
                f"（说明：『{subject}』和资料中的"
                f"『{'、'.join(other_aliases)}』是同一个人。）"
            )
        elif subject and display_name and subject != display_name:
            question = (
                f"{question}\n\n"
                f"（说明：『{subject}』和资料中的『{display_name}』是同一个人。）"
            )
        if mode is not None and mode.mode == "facet":
            label = CATEGORY_LABELS.get(
                str(mode.facet or ""),
                str(mode.facet or "指定方面"),
            )
            question = (
                f"{question}\n\n"
                f"（本次只回答“{label}”方面，不要展开其他方面。）"
            )
        elif mode is not None:
            categories = [
                CATEGORY_LABELS[item.get("category")]
                for item in (facts or [])
                if item.get("category") in CATEGORY_LABELS
            ]
            categories = list(dict.fromkeys(categories))
            instruction = (
                "本次是概览问题，请覆盖所有已获得的事实。"
                + (
                    f"当前已获得这些类别：{'、'.join(categories)}。"
                    if categories
                    else "当前没有结构化事实，只能依据可见对话归纳。"
                )
                + "没有的类别不要编造。"
            )
            question = f"{question}\n\n（{instruction}）"
        return question

    @staticmethod
    def _evidence_citations(
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates = [
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "quote": str(item.get("quote") or item.get("snippet") or ""),
            }
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        ]
        return _known_knowledge_citations(candidates, evidence)

    @staticmethod
    def _section_facts(
        facts: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Add categorized cache evidence to the structured fact set."""

        output: list[dict[str, Any]] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault(
                "attribution",
                (
                    "subject_said"
                    if copied.get("speaker_role") == "subject"
                    else "other_said"
                ),
            )
            output.append(copied)
        seen = {
            (
                str(item.get("category") or ""),
                str(item.get("normalized_value") or ""),
            )
            for item in output
        }
        for item in evidence:
            if not isinstance(item, dict):
                continue
            cache_fact = item.get("fetch_method") == "person_cache"
            if not cache_fact:
                continue
            value = str(item.get("fact_value") or "").strip()
            if not value:
                continue
            category = str(
                item.get("fact_category")
                or FACT_TYPE_TO_CATEGORY.get(str(item.get("fact_type") or ""))
                or "other"
            )
            fact_type = str(item.get("fact_type") or "cache")
            label = str(item.get("fact_label") or "")
            normalized = normalize_value(value)
            key = (category, normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            attribution = str(
                item.get("attribution")
                or "subject_said"
            )
            output.append(
                {
                    "category": category,
                    "canonical_key": f"{category}.{fact_type}",
                    "fact_type": fact_type,
                    "label": label,
                    "value": value,
                    "normalized_value": normalized,
                    "quote": str(item.get("quote") or value),
                    "speaker_role": (
                        "subject"
                        if attribution in {"subject_said", "profile"}
                        else "other"
                    ),
                    "attribution": attribution,
                    "resource_id": str(item.get("resource_id") or ""),
                    "sent_at": str(item.get("sent_at") or ""),
                    "status": "current",
                }
            )
        return output
