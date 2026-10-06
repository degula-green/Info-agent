"""Vision client: turn a picture into a short, information-dense description.

MinerU already reads the text out of a picture (a screenshot of a meeting note
comes back as ordinary text blocks). This client exists for the pictures it
cannot read - a photo, a scene, a chart whose meaning is not written out - so
those attachments still carry something retrieval can match.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from app.infrastructure.http import HttpClient, IntegrationError, join_url


class VisionError(RuntimeError):
    """The description could not be produced; callers degrade, never fail."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


DESCRIPTION_PROMPT = (
    "请用中文描述这张图片的内容，供后续问答检索使用。要求：\n"
    "1. 先列出图片中可见的文字（逐字保留，不要改写）；\n"
    "2. 再描述图表、表格、界面元素的关键信息，包含数字；\n"
    "3. 照片类图片描述场景、对象和值得注意的细节；\n"
    "4. 只描述你确实看到的内容，不要推测或补全；\n"
    "5. 总长度 300 字以内，直接输出描述，不要加标题或前缀。"
)


class VisionClient:
    """OpenAI-compatible chat/completions call with one image attached."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "qwen-vl-max",
        http: HttpClient | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 600,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = str(model or "qwen-vl-max")
        self.http = http or HttpClient()
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self.max_output_tokens = max(int(max_output_tokens), 1)

    def describe(self, image_bytes: bytes, mime_type: str) -> str:
        if not self.base_url:
            raise VisionError("vision base URL is not configured")
        if not image_bytes:
            raise VisionError("image is empty")
        mime = str(mime_type or "").strip() or "image/jpeg"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIPTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "max_tokens": self.max_output_tokens,
        }
        try:
            result = self.http.request(
                "POST",
                join_url(self.base_url, "chat/completions"),
                body=payload,
                timeout=self.timeout_seconds,
                token=self.api_key or None,
            )
        except IntegrationError as exc:
            raise VisionError("vision request failed", retryable=exc.retryable) from exc
        try:
            body = result.json()
        except IntegrationError as exc:
            raise VisionError("vision response is not valid JSON") from exc
        return _extract_text(body)


def _extract_text(body: Any) -> str:
    if not isinstance(body, Mapping):
        raise VisionError("vision response is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise VisionError("vision response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise VisionError("vision response choice is not an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise VisionError("vision response has no message")
    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts instead of one string.
        content = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping)
        )
    text = str(content or "").strip()
    if not text:
        raise VisionError("vision response is empty")
    return text
