from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url


router = APIRouter(prefix="/api/v1", tags=["contact-profile"])


class ContactProfileRequest(BaseModel):
    owner_user_id: str = Field(min_length=1, max_length=128)
    contact_key: str = Field(min_length=1, max_length=128)
    lines: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("lines")
    @classmethod
    def normalize_lines(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text:
                normalized.append(text[:4000])
        return normalized


def _authorize(authorization: str | None, caller_service: str | None) -> None:
    if not hmac.compare_digest((caller_service or "").strip(), "knowledge"):
        raise HTTPException(status_code=403, detail="caller is not authorized")
    parts = (authorization or "").split()
    expected = settings.knowledge_api_token
    if (
        not expected
        or len(parts) != 2
        or parts[0].lower() != "bearer"
        or not hmac.compare_digest(parts[1], expected)
    ):
        raise HTTPException(status_code=403, detail="caller is not authorized")


def _extract_answer(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"].strip()
    return ""


@router.post("/contact-profile/summarize")
def summarize_contact_profile(
    body: ContactProfileRequest,
    authorization: str | None = Header(default=None),
    x_caller_service: str | None = Header(default=None),
) -> dict[str, str]:
    _authorize(authorization, x_caller_service)
    if not body.lines:
        return {"summary": "暂无足够信息"}
    if not settings.qa_api_base_url or not settings.qa_api_key or not settings.qa_model:
        raise HTTPException(status_code=503, detail="profile provider is not configured")
    context = "\n".join(f"- {line}" for line in body.lines)
    context = context[: max(1000, settings.qa_max_context_tokens * 4)]
    payload = {
        "model": settings.qa_model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": min(500, max(100, settings.qa_max_output_tokens)),
        "messages": [
            {
                "role": "system",
                "content": "仅根据提供的联系人消息生成一段简洁、客观的中文画像。不得编造，不得补充外部信息。",
            },
            {
                "role": "user",
                "content": f"联系人消息：\n{context}\n\n请生成画像简介：",
            },
        ],
    }
    try:
        result = HttpClient().request(
            "POST",
            join_url(settings.qa_api_base_url, "/chat/completions"),
            body=payload,
            token=settings.qa_api_key,
            timeout=settings.qa_timeout_seconds,
        ).json()
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail="profile provider unavailable") from exc
    summary = _extract_answer(result)
    if not summary:
        raise HTTPException(status_code=503, detail="profile provider returned an empty summary")
    return {"summary": summary}
