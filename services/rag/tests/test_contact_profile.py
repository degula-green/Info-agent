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
    assert "项目" in messages[0]["content"]
    assert "会话名称" in messages[1]["content"]
    assert "A 项目群" in messages[1]["content"]
    assert body["max_tokens"] >= 1200


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
