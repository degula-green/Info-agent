from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url


router = APIRouter(prefix="/api/v1", tags=["contact-profile"])
NO_USEFUL_PROFILE = "__NO_USEFUL_PROFILE__"
# The profile runs on a dedicated non-reasoning model. The output is three
# short sections, so a small budget is enough and the "reasoning ate the whole
# budget" empty-answer failure cannot happen.
PROFILE_MAX_OUTPUT_TOKENS = 600


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


def _summarize(payload: dict[str, Any]) -> str:
    """One provider call; the answer text it produced, possibly empty."""

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
    return _extract_answer(result)


@router.post("/contact-profile/summarize")
def summarize_contact_profile(
    body: ContactProfileRequest,
    authorization: str | None = Header(default=None),
    x_caller_service: str | None = Header(default=None),
) -> dict[str, str]:
    _authorize(authorization, x_caller_service)
    if not body.lines:
        return {"summary": ""}
    if (
        not settings.qa_api_base_url
        or not settings.qa_api_key
        or not (settings.qa_profile_model or settings.qa_model)
    ):
        raise HTTPException(status_code=503, detail="profile provider is not configured")
    context = "\n".join(f"- {line}" for line in body.lines)
    max_context_chars = max(1000, settings.qa_max_context_tokens * 4)
    if len(context) > max_context_chars:
        # Keep the newest evidence when the contact has accumulated a long
        # message history.
        context = context[-max_context_chars:]
    payload = {
        "model": settings.qa_profile_model or settings.qa_model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": PROFILE_MAX_OUTPUT_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是联系人画像整理器。只根据提供的联系人消息，输出固定三段：\n"
                    "1. 是谁：这个人是谁（能确定的姓名、公司等身份信息）；\n"
                    "2. 什么职务：他的职位或角色；\n"
                    "3. 近期负责什么：近期负责的项目或任务。\n"
                    "硬性要求：\n"
                    "1. 只写工作相关内容；闲聊、问候、表情、玩笑、家庭、生活、"
                    "农活、娱乐、出行等私人内容一律不写。\n"
                    "2. 只写消息里明确出现的事实，不推断，不补充外部信息。\n"
                    "3. 固定输出三行：是谁 / 什么职务 / 近期负责什么；"
                    "某项没有信息就写“未知”；总长度 150 字以内。\n"
                    "4. 不要复述消息原文，要归纳。\n"
                    f"5. 三项都没有时只返回 {NO_USEFUL_PROFILE}，不要输出解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"联系人发送的消息（已标注时间、会话类型和会话名称）：\n{context}\n\n"
                    "请按上述要求输出这个人的画像（是谁 / 什么职务 / 近期负责什么）："
                ),
            },
        ],
    }
    summary = _summarize(payload)
    if summary.strip().strip("`").upper() == NO_USEFUL_PROFILE:
        return {"summary": ""}
    if not summary:
        raise HTTPException(status_code=503, detail="profile provider returned an empty summary")
    return {"summary": summary}
