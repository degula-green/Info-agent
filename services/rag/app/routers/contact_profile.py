from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url


router = APIRouter(prefix="/api/v1", tags=["contact-profile"])
NO_USEFUL_PROFILE = "__NO_USEFUL_PROFILE__"
# The summariser runs on a reasoning-backed model: it thinks first and answers
# afterwards, and on a long contact history the thinking alone can consume the
# whole budget, leaving an empty answer string that reads as "provider
# unavailable". A 142-message contact burned all 1200 tokens on reasoning;
# 3000 leaves room for both passes.
PROFILE_MIN_OUTPUT_TOKENS = 3000
PROFILE_MAX_OUTPUT_TOKENS = 6000


def _profile_max_tokens() -> int:
    configured = int(settings.qa_max_output_tokens or 0)
    return min(PROFILE_MAX_OUTPUT_TOKENS, max(PROFILE_MIN_OUTPUT_TOKENS, configured))


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
    if not settings.qa_api_base_url or not settings.qa_api_key or not settings.qa_model:
        raise HTTPException(status_code=503, detail="profile provider is not configured")
    context = "\n".join(f"- {line}" for line in body.lines)
    max_context_chars = max(1000, settings.qa_max_context_tokens * 4)
    if len(context) > max_context_chars:
        # Keep the newest evidence when the contact has accumulated a long
        # message history.
        context = context[-max_context_chars:]
    payload = {
        "model": settings.qa_model,
        "stream": False,
        "temperature": 0.2,
        # Reasoning-backed models can consume most of the response budget before
        # emitting visible content. Keep enough room for both.
        "max_tokens": _profile_max_tokens(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是联系人信息整理器。只根据提供的联系人消息，提炼两类关键信息：\n"
                    "1. 近期任务：该联系人正在做、承诺要做或参与推进的具体事项，"
                    "写明事项、他的角色和当前进展；\n"
                    "2. 达成的共识：消息中明确确认的结论、约定或决定。\n"
                    "硬性要求：\n"
                    "1. 只保留任务与共识。闲聊、问候、表情、玩笑、吐槽、生活琐事、"
                    "与工作无关的寒暄一律不写。\n"
                    "2. 只写消息里明确出现的事实，不推断，不补充外部信息。\n"
                    "3. 尽可能简洁：每条以“- ”开头、一行说完；同一事项多次出现要合并成一条；"
                    "总长度控制在 150 字以内。\n"
                    "4. 按“近期任务”“达成的共识”两组输出，标题单独一行；"
                    "某一组没有内容就不输出该标题。\n"
                    "5. 不要复述消息原文，要归纳。\n"
                    f"6. 既没有任务也没有共识时，只返回 {NO_USEFUL_PROFILE}，不要输出解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"联系人发送的消息（已标注时间、会话类型和会话名称）：\n{context}\n\n"
                    "请按上述要求输出该联系人近期的任务与达成的共识（简洁，只保留关键信息）："
                ),
            },
        ],
    }
    summary = _summarize(payload)
    if not summary.strip() and payload["max_tokens"] < PROFILE_MAX_OUTPUT_TOKENS:
        # The reasoning pass ate the whole budget on this attempt -- it varies
        # run to run, so one retry at the ceiling is what makes this endpoint
        # dependable instead of 50/50.
        payload["max_tokens"] = PROFILE_MAX_OUTPUT_TOKENS
        summary = _summarize(payload)
    if summary.strip().strip("`").upper() == NO_USEFUL_PROFILE:
        return {"summary": ""}
    if not summary:
        raise HTTPException(status_code=503, detail="profile provider returned an empty summary")
    return {"summary": summary}
