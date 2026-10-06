from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.routers import contact_profile


def test_contact_profile_prompt_prioritizes_projects(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def json(self):
            return {
                "choices": [
                    {"message": {"content": "参与过 A 项目，负责后端接口和跨团队协作。"}}
                ]
            }

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            captured.update(
                {"method": method, "url": url, "body": body, "token": token, "timeout": timeout}
            )
            return FakeResponse()

    monkeypatch.setattr(
        contact_profile,
        "settings",
        replace(
            contact_profile.settings,
            knowledge_api_token="rag-token",
            qa_api_base_url="https://qa.example",
            qa_api_key="qa-key",
            qa_model="qa-model",
            qa_timeout_seconds=10.0,
            qa_max_context_tokens=6000,
            qa_max_output_tokens=1200,
        ),
    )
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    result = contact_profile.summarize_contact_profile(
        contact_profile.ContactProfileRequest(
            owner_user_id="owner",
            contact_key="contact",
            lines=["[2026-10-04 10:00 | 群聊 | A 项目群] 我负责后端接口和排期"],
        ),
        authorization="Bearer rag-token",
        x_caller_service="knowledge",
    )

    assert result["summary"].startswith("参与过 A 项目")
    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    # The profile is a task/consensus brief, not an activity log.
    assert "近期任务" in messages[0]["content"]
    assert "共识" in messages[0]["content"]
    assert "闲聊" in messages[0]["content"]
    assert "会话名称" in messages[1]["content"]
    assert "A 项目群" in messages[1]["content"]
    # A reasoning-backed model needs room for its thinking plus the answer.
    assert body["max_tokens"] >= 3000


def test_contact_profile_hides_when_model_has_no_useful_information(monkeypatch) -> None:
    class FakeResponse:
        def json(self):
            return {
                "choices": [
                    {"message": {"content": "__NO_USEFUL_PROFILE__"}}
                ]
            }

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(
        contact_profile,
        "settings",
        replace(
            contact_profile.settings,
            knowledge_api_token="rag-token",
            qa_api_base_url="https://qa.example",
            qa_api_key="qa-key",
            qa_model="qa-model",
            qa_timeout_seconds=10.0,
        ),
    )
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    result = contact_profile.summarize_contact_profile(
        contact_profile.ContactProfileRequest(
            owner_user_id="owner",
            contact_key="contact",
            lines=["[2026-10-04 10:00 | 私聊 | 未命名会话] 110 119 120"],
        ),
        authorization="Bearer rag-token",
        x_caller_service="knowledge",
    )

    assert result == {"summary": ""}


def test_contact_profile_retries_once_when_the_model_returns_no_answer(monkeypatch) -> None:
    """A reasoning pass can eat the whole budget; retry once at the ceiling."""

    budgets: list[int] = []

    class FakeResponse:
        def __init__(self, content: str) -> None:
            self._content = content

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": self._content,
                            "reasoning_content": "思考过程",
                        }
                    }
                ]
            }

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            budgets.append(int(body["max_tokens"]))
            # First call: the model thought itself out of a budget.
            return FakeResponse("" if len(budgets) == 1 else "- 官网部署进行中")

    monkeypatch.setattr(
        contact_profile,
        "settings",
        replace(
            contact_profile.settings,
            knowledge_api_token="rag-token",
            qa_api_base_url="https://qa.example",
            qa_api_key="qa-key",
            qa_model="qa-model",
            qa_timeout_seconds=10.0,
            qa_max_context_tokens=6000,
            qa_max_output_tokens=1200,
        ),
    )
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    result = contact_profile.summarize_contact_profile(
        contact_profile.ContactProfileRequest(
            owner_user_id="owner",
            contact_key="contact",
            lines=["[2026-10-05] 小马: 官网部署我来做"],
        ),
        authorization="Bearer rag-token",
        x_caller_service="knowledge",
    )

    assert result["summary"] == "- 官网部署进行中"
    assert budgets == [
        contact_profile.PROFILE_MIN_OUTPUT_TOKENS,
        contact_profile.PROFILE_MAX_OUTPUT_TOKENS,
    ]
