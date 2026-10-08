from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from app.routers import contact_profile


def _settings():
    return replace(
        contact_profile.settings,
        knowledge_api_token="rag-token",
        qa_api_base_url="https://qa.example",
        qa_api_key="qa-key",
        qa_model="qa-model",
        qa_profile_model="profile-model",
        qa_timeout_seconds=10.0,
        qa_max_context_tokens=6000,
        qa_max_output_tokens=1200,
    )


def _request() -> contact_profile.ContactProfileRequest:
    return contact_profile.ContactProfileRequest(
        owner_user_id="owner",
        contact_key="contact",
        lines=["[2026-10-04 10:00 | 群聊 | A 项目群] 我负责后端接口和排期"],
    )


def test_contact_profile_uses_dedicated_model_and_three_sections(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "是谁：小李\n什么职务：后端负责人\n近期负责什么：官网部署"
                        }
                    }
                ]
            }

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            captured["body"] = body
            return FakeResponse()

    monkeypatch.setattr(contact_profile, "settings", _settings())
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    result = contact_profile.summarize_contact_profile(
        _request(),
        authorization="Bearer rag-token",
        x_caller_service="knowledge",
    )

    assert result["summary"].startswith("是谁：")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "profile-model"
    assert body["max_tokens"] <= 600
    system = body["messages"][0]["content"]
    for section in ("是谁", "什么职务", "近期负责什么"):
        assert section in system
    assert "闲聊" in system


def test_contact_profile_hides_when_model_has_no_useful_information(monkeypatch) -> None:
    class FakeResponse:
        def json(self):
            return {"choices": [{"message": {"content": "__NO_USEFUL_PROFILE__"}}]}

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(contact_profile, "settings", _settings())
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    result = contact_profile.summarize_contact_profile(
        _request(),
        authorization="Bearer rag-token",
        x_caller_service="knowledge",
    )

    assert result == {"summary": ""}


def test_contact_profile_does_not_retry_on_empty_answer(monkeypatch) -> None:
    calls: list[int] = []

    class FakeResponse:
        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    class FakeHttpClient:
        def request(self, method, url, *, body=None, token=None, timeout=None):
            calls.append(1)
            return FakeResponse()

    monkeypatch.setattr(contact_profile, "settings", _settings())
    monkeypatch.setattr(contact_profile, "HttpClient", FakeHttpClient)

    with pytest.raises(HTTPException) as excinfo:
        contact_profile.summarize_contact_profile(
            _request(),
            authorization="Bearer rag-token",
            x_caller_service="knowledge",
        )

    assert excinfo.value.status_code == 503
    assert len(calls) == 1
