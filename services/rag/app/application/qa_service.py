from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from app.application.mvp_ports import AnswerProvider, QAHistoryRepository
from app.application.rag_service import RAGRetrievalService
from app.domain.rag import SearchRequest, SearchResult


class ConversationNotFound(RuntimeError):
    pass


class ConversationScopeMismatch(RuntimeError):
    pass


class UnsupportedConversationOperation(RuntimeError):
    pass


INSUFFICIENT_CONTEXT_ANSWER = "未找到足够相关的资料，暂时无法回答。"


class QAService:
    def __init__(
        self,
        *,
        repository: QAHistoryRepository,
        retrieval_service: RAGRetrievalService,
        answer_provider: AnswerProvider,
    ) -> None:
        self.repository = repository
        self.retrieval_service = retrieval_service
        self.answer_provider = answer_provider

    def list_conversations(
        self,
        *,
        user_id: str,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        return self.repository.list_qa_conversations(
            user_id=user_id,
            page=page,
            page_size=page_size,
        )

    def create_conversation(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        title: str | None,
        retrieval_mode: str,
        knowledge_base_ids: list[str],
    ) -> str:
        return self.repository.create_qa_conversation(
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            title=title,
            retrieval_mode=retrieval_mode,
            knowledge_base_ids=knowledge_base_ids,
        )

    def get_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        value = self.repository.get_qa_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not value:
            raise ConversationNotFound("conversation not found")
        return value

    def rename_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        title: str,
    ) -> bool:
        return self.repository.rename_qa_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
        )

    def delete_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        return self.repository.delete_qa_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )

    def answer(self, request: SearchRequest) -> dict[str, Any]:
        conversation_id, user_message_id = self._start_question(request)
        assistant_message_id = self._create_assistant_message(
            conversation_id=conversation_id,
            status="streaming",
        )
        started = time.perf_counter()
        try:
            response = self.retrieval_service.search(request)
            answer = (
                self.answer_provider.generate(request.query, response.results)
                if response.results
                else INSUFFICIENT_CONTEXT_ANSWER
            )
            citations = _citations(response.results)
            self.repository.update_qa_message(
                assistant_message_id,
                content=answer,
                citations=citations,
                status="completed",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            self.repository.update_qa_message(
                assistant_message_id,
                content="",
                status="failed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=type(exc).__name__,
                error_stage="answer",
                error_class=type(exc).__name__,
                error_message=str(exc)[:500],
                retryable=True,
            )
            raise
        return {
            "request_id": response.request_id,
            "conversation_id": conversation_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "answer": answer,
            "citations": citations,
            "items": [_legacy_item(item) for item in response.results],
            "diagnostics": response.diagnostics,
            "retrieval_mode": request.qa_mode,
            "execution_path": response.diagnostics["effective_execution_path"],
        }

    def answer_stream(
        self,
        request: SearchRequest,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        conversation_id, user_message_id = self._start_question(request)
        assistant_message_id = self._create_assistant_message(
            conversation_id=conversation_id,
            status="streaming",
        )
        yield (
            "meta",
            {
                "conversation_id": conversation_id,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
            },
        )
        started = time.perf_counter()
        tokens: list[str] = []
        try:
            response = self.retrieval_service.search(request)
            citations = _citations(response.results)
            for citation in citations:
                yield ("citation", {"citation": citation})
            if response.results:
                for delta in self.answer_provider.generate_stream(
                    request.query,
                    response.results,
                ):
                    if delta:
                        tokens.append(delta)
                        yield ("token", {"delta": delta})
            else:
                tokens.append(INSUFFICIENT_CONTEXT_ANSWER)
                yield ("token", {"delta": INSUFFICIENT_CONTEXT_ANSWER})
            answer = "".join(tokens)
            self.repository.update_qa_message(
                assistant_message_id,
                content=answer,
                citations=citations,
                status="completed",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            yield (
                "done",
                {
                    "request_id": response.request_id,
                    "conversation_id": conversation_id,
                    "user_message_id": user_message_id,
                    "assistant_message_id": assistant_message_id,
                    "answer": answer,
                    "citations": citations,
                    "diagnostics": response.diagnostics,
                    "execution_path": response.diagnostics[
                        "effective_execution_path"
                    ],
                },
            )
        except Exception as exc:
            self.repository.update_qa_message(
                assistant_message_id,
                content="".join(tokens),
                status="failed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=type(exc).__name__,
                error_stage="stream",
                error_class=type(exc).__name__,
                error_message=str(exc)[:500],
                retryable=True,
            )
            yield ("error", {"code": "qa_unavailable", "message": str(exc)})
            yield (
                "done",
                {
                    "conversation_id": conversation_id,
                    "user_message_id": user_message_id,
                    "assistant_message_id": assistant_message_id,
                    "answer": None,
                    "diagnostics": {},
                },
            )

    def _start_question(self, request: SearchRequest) -> tuple[str, str]:
        conversation_id = request.conversation_id
        if conversation_id:
            conversation = self.repository.get_qa_conversation(
                user_id=request.user_id,
                conversation_id=conversation_id,
            )
            if not conversation:
                raise ConversationNotFound("conversation not found")
            if (
                conversation.get("scope_type") != request.scope_type
                or str(conversation.get("scope_id")) != request.scope_id
            ):
                raise ConversationScopeMismatch("conversation scope mismatch")
        else:
            conversation_id = self.repository.create_qa_conversation(
                user_id=request.user_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                title=(request.query.strip()[:50] or "新的对话"),
                retrieval_mode=request.qa_mode,
                knowledge_base_ids=list(request.knowledge_base_ids),
            )
        user_message_id = self.repository.add_qa_message(
            conversation_id=conversation_id,
            role="user",
            content=request.query,
            status="completed",
        )
        return conversation_id, user_message_id

    def _create_assistant_message(
        self,
        *,
        conversation_id: str,
        status: str,
    ) -> str:
        return self.repository.add_qa_message(
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status=status,
        )


def _citations(results: list[SearchResult]) -> list[dict[str, Any]]:
    return [
        _citation(result, index)
        for index, result in enumerate(results, start=1)
    ]


def _citation(result: SearchResult, rank: int) -> dict[str, Any]:
    item = _legacy_item(result)
    resource_type = str(item.get("resource_type") or "message")
    is_document = resource_type == "attachment"
    message_id = (
        item.get("message_id")
        or item.get("source_message_id")
        or (item.get("resource_id") if not is_document else None)
    )
    attachment_id = (
        item.get("attachment_id")
        or item.get("document_id")
        or item.get("resource_id")
        if is_document
        else None
    )
    knowledge_item_id = item.get("knowledge_item_id")
    conversation_id = item.get("conversation_id") or item.get("source_conversation_id")
    citation_id = (
        f"attachment:{attachment_id}"
        if attachment_id
        else f"message:{message_id}"
        if message_id
        else f"knowledge:{knowledge_item_id}"
        if knowledge_item_id
        else f"chunk:{result.chunk_id}"
    )
    return {
        "rank": rank,
        "citation_id": citation_id,
        "type": "document" if is_document else "message",
        "source_kind": "document" if is_document else "message",
        "resource_type": resource_type,
        "resource_id": item.get("resource_id"),
        "knowledge_item_id": knowledge_item_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "attachment_id": attachment_id,
        "document_id": attachment_id,
        "platform": item.get("platform") or item.get("source_platform"),
        "file_name": item.get("file_name"),
        "title": item.get("title"),
        "snippet": result.content,
        "content_variant": item.get("content_variant"),
    }


def _legacy_item(result: SearchResult) -> dict[str, Any]:
    item = result.safe_dict()
    source = item.pop("source", {}) or {}
    return {
        **source,
        **item,
        "source": source.get("platform")
        or source.get("resource_type")
        or "knowledge",
    }
