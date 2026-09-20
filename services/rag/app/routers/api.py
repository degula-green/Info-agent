from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.domain.models import SearchRequest
from app.domain.memory import TreeSearchRequest
from app.config import settings
from app.infrastructure.elasticsearch import ElasticsearchUnavailable
from app.schemas.search import AIDocumentBody, SearchBody, TreeSearchBody
from app.services.search_service import PreparedAnswer, QAConversationNotFound, QAHistoryUnavailable, get_service
from app.infrastructure.qa import OpenAICompatibleAnswerProvider, QAUnavailable
from app.services.memory_search_service import get_tree_search_service


router = APIRouter(prefix="/api/v1", tags=["rag"])


class QATitleBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class QAConversationBody(BaseModel):
    title: str = Field(default="新的对话", min_length=1, max_length=300)
    retrieval_mode: str = Field(default="quick", pattern="^(quick|deep)$")
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    organization_id: str | None = None

    @field_validator("knowledge_base_ids")
    @classmethod
    def clean_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def _user_id(header_user_id: str | None) -> str:
    value = (header_user_id or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="X-User-ID header is required")
    return value


def _request(body: SearchBody, *, entry: str, header_user_id: str | None) -> SearchRequest:
    if settings.environment not in {"development", "test"} and not (header_user_id or "").strip():
        raise HTTPException(status_code=401, detail="X-User-ID header is required")
    user_id = (header_user_id or body.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="user identity is required")
    return SearchRequest(
        query=body.query,
        user_id=user_id,
        organization_id=body.organization_id,
        knowledge_base_id=body.knowledge_base_id,
        knowledge_base_ids=tuple(body.knowledge_base_ids),
        entry=entry,
        sender_name=body.sender_name,
        occurred_after=body.occurred_after,
        occurred_before=body.occurred_before,
        top_k=body.top_k,
        include_protected=body.include_protected,
        conversation_id=getattr(body, "conversation_id", None),
        qa_mode=getattr(body, "mode", None),
    )


@router.post("/search/global")
def global_search(body: SearchBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    return _run_search(_request(body, entry="global", header_user_id=x_user_id))


@router.post("/search/knowledge")
def knowledge_search(body: SearchBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    return _run_search(_request(body, entry="knowledge", header_user_id=x_user_id))


@router.post("/search/tree")
def tree_search(body: TreeSearchBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    user_id = (x_user_id or body.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="user identity is required")
    invalid = set(body.tree_types) - {"session", "entity"}
    if invalid:
        raise HTTPException(status_code=422, detail="tree_types supports session and entity only")
    knowledge_base_ids = tuple(body.knowledge_base_ids) or ((body.knowledge_base_id,) if body.knowledge_base_id else ())
    if not knowledge_base_ids:
        raise HTTPException(status_code=422, detail="knowledge_base_id or knowledge_base_ids is required")
    request = TreeSearchRequest(query=body.query, user_id=user_id, organization_id=body.organization_id, knowledge_base_id=body.knowledge_base_id, knowledge_base_ids=knowledge_base_ids, tree_types=tuple(body.tree_types), top_k=body.top_k, include_protected=body.include_protected)
    try:
        return get_tree_search_service().search(request)
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="tree search backend unavailable") from exc


@router.post("/ai/documents")
def ai_documents(body: AIDocumentBody, x_user_id: str | None = Header(default=None)) -> dict[str, object]:
    request = _request(body, entry="ai", header_user_id=x_user_id)
    try:
        return get_service().answer(request)
    except QAConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except QAHistoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="RAG request could not be completed") from exc


@router.post("/ai/documents/stream")
def ai_documents_stream(body: AIDocumentBody, x_user_id: str | None = Header(default=None)) -> StreamingResponse:
    request = _request(body, entry="ai", header_user_id=x_user_id)

    def events() -> Iterator[str]:
        try:
            service = get_service()
            prepared = service.prepare_answer(request)
            if not isinstance(prepared, PreparedAnswer):
                yield _sse("meta", {"conversation_id": prepared.get("conversation_id"), "user_message_id": prepared.get("user_message_id")})
                yield _sse("error", {"code": prepared.get("error_code") or "qa_unavailable", "message": "AI 问答暂时不可用"})
                yield _sse("done", {"assistant_message_id": prepared.get("assistant_message_id"), "answer": None, "citations": prepared.get("citations", []), "diagnostics": prepared.get("diagnostics", {})})
                return
            yield _sse("meta", {"conversation_id": prepared.conversation_id, "user_message_id": prepared.user_message_id})
            for citation in prepared.context.citations:
                yield _sse("citation", {"citation": citation})
            provider = service.answer_provider or OpenAICompatibleAnswerProvider()
            parts: list[str] = []
            for delta in provider.generate_stream(prepared.request.query, prepared.results):
                if delta:
                    parts.append(delta)
                    yield _sse("token", {"delta": delta})
            answer = "".join(parts).strip()
            if not answer:
                raise QAUnavailable("QA provider returned an empty streamed answer")
            result = service.complete_answer(prepared, answer)
            yield _sse("done", {"assistant_message_id": result.get("assistant_message_id"), "answer": answer, "citations": result.get("citations", []), "diagnostics": result.get("diagnostics", {}), "retrieval_mode": result.get("retrieval_mode")})
        except QAConversationNotFound:
            yield _sse("error", {"code": "conversation_not_found", "message": "问答会话不存在或无权访问"})
            yield _sse("done", {"assistant_message_id": None, "answer": None, "citations": [], "diagnostics": {}})
        except QAHistoryUnavailable:
            yield _sse("error", {"code": "qa_history_unavailable", "message": "问答历史暂时无法保存，请稍后重试"})
            yield _sse("done", {"assistant_message_id": None, "answer": None, "citations": [], "diagnostics": {}})
        except Exception as exc:
            if "prepared" in locals() and isinstance(prepared, PreparedAnswer):
                try:
                    service.fail_answer(prepared, exc)
                except Exception:
                    pass
            yield _sse("error", {"code": "qa_unavailable", "message": str(exc)})
            yield _sse("done", {"assistant_message_id": None, "answer": None, "citations": [], "diagnostics": {}})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/qa/conversations")
def list_qa_conversations(x_user_id: str | None = Header(default=None), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    user_id = _user_id(x_user_id)
    try:
        items, total = get_service().repository.list_qa_conversations(user_id=user_id, page=page, page_size=page_size)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.post("/qa/conversations")
def create_qa_conversation(body: QAConversationBody | None = None, x_user_id: str | None = Header(default=None)) -> dict[str, Any]:
    user_id = _user_id(x_user_id)
    value = body or QAConversationBody()
    try:
        conversation_id = get_service().repository.create_qa_conversation(
            user_id=user_id,
            organization_id=value.organization_id,
            title=value.title,
            retrieval_mode=value.retrieval_mode,
            knowledge_base_ids=value.knowledge_base_ids,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    return {"id": conversation_id, "title": value.title, "retrieval_mode": value.retrieval_mode, "knowledge_base_ids": value.knowledge_base_ids, "message_count": 0}


@router.get("/qa/conversations/{conversation_id}")
def get_qa_conversation(conversation_id: str, x_user_id: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        value = get_service().repository.get_qa_conversation(user_id=_user_id(x_user_id), conversation_id=conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    if not value: raise HTTPException(status_code=404, detail="conversation not found")
    return value


@router.patch("/qa/conversations/{conversation_id}")
def rename_qa_conversation(conversation_id: str, body: QATitleBody, x_user_id: str | None = Header(default=None)) -> dict[str, Any]:
    user_id = _user_id(x_user_id)
    try:
        renamed = get_service().repository.rename_qa_conversation(user_id=user_id, conversation_id=conversation_id, title=body.title)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    if not renamed:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"id": conversation_id, "title": body.title}


@router.delete("/qa/conversations/{conversation_id}")
def delete_qa_conversation(conversation_id: str, x_user_id: str | None = Header(default=None)) -> dict[str, str]:
    try:
        deleted = get_service().repository.delete_qa_conversation(user_id=_user_id(x_user_id), conversation_id=conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QA history persistence unavailable") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"status": "deleted"}


def _run_search(request: SearchRequest) -> dict[str, object]:
    try:
        response = get_service().search(request)
    except ElasticsearchUnavailable as exc:
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="RAG request could not be completed") from exc
    return {
        "request_id": response.request_id,
        "query": request.query,
        "items": [item.as_dict() for item in response.results],
        "diagnostics": {
            "protected_scope_available": response.diagnostics.protected_scope_available,
            "protected_scope_count": response.diagnostics.protected_scope_count,
            "candidate_count": response.diagnostics.candidate_count,
            "authorized_count": response.diagnostics.authorized_count,
            "degraded": list(response.diagnostics.degraded),
            "use_vector": response.diagnostics.plan.use_vector,
        },
    }


def _sse(event: str, value: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
