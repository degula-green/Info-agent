"""Laya and hybrid understanding provider tests."""

from __future__ import annotations

import pytest

from app.infrastructure.http import HttpResult, IntegrationError
from app.infrastructure.laya.client import (
    HttpLayaClient,
    HttpSystemOneClient,
    LayaError,
    systemone_url,
)
from app.container import build_understanding_provider
from app.ingress.chat import ChatIngress
from app.kernel.models import TaskUnderstanding, UnderstandingIntent
from app.understanding.hybrid import HybridUnderstandingProvider
from app.understanding.laya import JevUnderstandingProvider, LayaUnderstandingProvider
from app.understanding.schema import INTENT_OPTION_ORDER
from tests.support import make_settings


def _response(
    label: str,
    probabilities: dict[str, float],
    *,
    answer_confidence: float | None = None,
) -> dict:
    return {
        "model": "laya-rl-agent",
        "answers": {
            "intent": {
                "type": "choice",
                "choice": label,
                "probabilities": probabilities,
                "answer_confidence": (
                    probabilities[label]
                    if answer_confidence is None
                    else answer_confidence
                ),
            }
        },
    }


class StubLayaClient:
    model = "multilingual"

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0
        self.states: list[object] = []
        self.questions: list[dict] = []

    def predict(self, state, questions, *, max_len=None, head_max_len=None):
        self.calls += 1
        self.states.append(state)
        self.questions.append(questions)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class StubFallback:
    model = "stub-llm"
    last_call_count = 0

    def __init__(self, result: TaskUnderstanding | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def understand(self, task, *, min_confidence=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        self.last_call_count = 1
        return self.result


class StubHttp:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return HttpResult(self.status, {}, self.body)


def _fallback_result() -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=True,
        goal="明天下午三点跟张三开评审会",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name="todo.create", confidence=0.93)
        ],
        confidence=0.93,
        reason="llm fallback",
    )


def test_laya_supported_intent_maps_to_task_understanding() -> None:
    client = StubLayaClient(
        _response("todo.create", {"todo.create": 0.91, "knowledge.answer": 0.02})
    )
    provider = LayaUnderstandingProvider(client)
    task = ChatIngress().create_task("user-1", {"text": "明天下午三点跟张三开评审会"})

    result = provider.understand(task)

    assert result.is_task is True
    assert result.task_kind == "action"
    assert result.intent_candidates[0].name == "todo.create"
    assert result.intent_candidates[0].confidence == pytest.approx(0.91)
    assert provider.last_call_count == 1
    assert provider.last_fallback_reason is None
    assert client.states == [{"text": "明天下午三点跟张三开评审会"}]


def test_http_laya_client_sends_the_expected_request() -> None:
    http = StubHttp(
        b'{"answers":{"intent":{"choice":"todo.create",'
        b'"probabilities":{"todo.create":1.0},"answer_confidence":1.0}}}'
    )
    client = HttpLayaClient(
        base_url="http://127.0.0.1:8110",
        api_key="token",
        model="multilingual",
        timeout_seconds=3.0,
        http=http,
    )

    result = client.predict({"text": "明天开会"}, {"intent": {"type": "choice"}})

    assert result["answers"]["intent"]["choice"] == "todo.create"
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8110/v1/systemone"
    assert call["token"] == "token"
    assert call["timeout"] == 3.0
    assert call["body"]["model"] == "multilingual"


def test_http_laya_client_rejects_invalid_json() -> None:
    client = HttpLayaClient(
        base_url="http://127.0.0.1:8110",
        http=StubHttp(b"not-json"),
    )

    with pytest.raises(LayaError):
        client.predict({"text": "明天开会"}, {"intent": {"type": "choice"}})


def test_container_builds_hybrid_provider() -> None:
    settings = make_settings(
        understanding_provider="hybrid",
        laya_base_url="http://127.0.0.1:8110",
        llm_base_url="http://127.0.0.1:9000/v1",
        llm_model="stub",
    )

    provider = build_understanding_provider(settings)

    assert provider.name == "hybrid"
    assert provider.laya.model == "laya:multilingual"
    assert provider.fallback.model == "stub"


def test_laya_non_task_and_other_task_are_accepted() -> None:
    non_task = LayaUnderstandingProvider(
        StubLayaClient(
            _response("non_task", {"non_task": 0.97, "todo.create": 0.01})
        )
    )
    other_task = LayaUnderstandingProvider(
        StubLayaClient(
            _response("other_task", {"other_task": 0.88, "todo.create": 0.02})
        )
    )
    task = ChatIngress().create_task("user-1", {"text": "晚上好"})

    non_task_result = non_task.understand(task)
    other_result = other_task.understand(
        ChatIngress().create_task("user-1", {"text": "帮我订高铁票"})
    )

    assert non_task_result.is_task is False
    assert non_task_result.intent_candidates == []
    assert other_result.is_task is True
    assert other_result.intent_candidates == []


def test_laya_form_complete_is_a_single_action_intent() -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(
            _response("form.complete", {"form.complete": 0.94, "other_task": 0.02})
        )
    )

    result = provider.understand(
        ChatIngress().create_task(
            "user-1",
            {"text": "根据这些资料把申请表填好，填完让我预览确认"},
        )
    )

    assert result.is_task is True
    assert result.task_kind == "action"
    assert [item.name for item in result.intent_candidates] == ["form.complete"]


def test_laya_keeps_submission_out_of_form_complete() -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(
            _response("other_task", {"other_task": 0.91, "form.complete": 0.03})
        )
    )

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "把填好的申请表提交到平台"})
    )

    assert result.is_task is True
    assert result.intent_candidates == []


@pytest.mark.parametrize("legacy", ["form.prepare", "form.submit", "document.compare"])
def test_laya_rejects_retired_labels(legacy: str) -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(_response(legacy, {legacy: 0.99}))
    )

    with pytest.raises(LayaError):
        provider.understand(ChatIngress().create_task("user-1", {"text": "填表"}))


def test_laya_rejects_probabilities_for_unknown_intents() -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(
            _response(
                "form.complete",
                {"form.complete": 0.90, "form.submit": 0.05},
            )
        )
    )

    with pytest.raises(LayaError):
        provider.understand(ChatIngress().create_task("user-1", {"text": "填表"}))


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            _response("todo.create", {"todo.create": 0.61, "knowledge.answer": 0.20}),
            "low_probability",
        ),
        (
            _response("todo.create", {"todo.create": 0.86, "knowledge.answer": 0.80}),
            "low_margin",
        ),
        (
            _response("todo.create", {"todo.create": 0.96, "web.research": 0.80}),
            "multiple_intents",
        ),
    ],
)
def test_laya_marks_uncertain_results(response: dict, reason: str) -> None:
    provider = LayaUnderstandingProvider(StubLayaClient(response))

    evaluation = provider.evaluate(
        ChatIngress().create_task("user-1", {"text": "查一下这个政策"})
    )

    assert evaluation.accepted is False
    assert evaluation.fallback_reason == reason
    assert evaluation.understanding.is_task is True
    assert evaluation.understanding.intent_candidates == []


def test_laya_provider_threshold_cannot_be_lowered_by_runtime() -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(
            _response("todo.create", {"todo.create": 0.82, "knowledge.answer": 0.02})
        ),
        min_confidence=0.90,
    )

    evaluation = provider.evaluate(
        ChatIngress().create_task("user-1", {"text": "提醒我交房租"}),
        min_confidence=0.70,
    )

    assert evaluation.accepted is False
    assert evaluation.fallback_reason == "low_probability"


def test_laya_rejects_unknown_labels() -> None:
    provider = LayaUnderstandingProvider(
        StubLayaClient(_response("unknown.intent", {"unknown.intent": 0.99}))
    )

    with pytest.raises(LayaError):
        provider.understand(ChatIngress().create_task("user-1", {"text": "你好"}))


def test_hybrid_uses_laya_without_calling_llm() -> None:
    fallback = StubFallback(_fallback_result())
    provider = HybridUnderstandingProvider(
        laya=LayaUnderstandingProvider(
            StubLayaClient(
                _response("todo.create", {"todo.create": 0.94, "web.research": 0.02})
            )
        ),
        fallback=fallback,
    )

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天下午三点开会"})
    )

    assert result.intent_candidates[0].name == "todo.create"
    assert provider.last_decision_source == "laya"
    assert provider.last_call_count == 1
    assert fallback.calls == 0


def test_hybrid_falls_back_on_uncertain_result() -> None:
    fallback = StubFallback(_fallback_result())
    provider = HybridUnderstandingProvider(
        laya=LayaUnderstandingProvider(
            StubLayaClient(
                _response("todo.create", {"todo.create": 0.60, "web.research": 0.20})
            )
        ),
        fallback=fallback,
    )

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天下午三点开会"})
    )

    assert result.reason == "llm fallback"
    assert provider.last_decision_source == "llm"
    assert provider.last_fallback_reason == "low_probability"
    assert provider.last_call_count == 2
    assert fallback.calls == 1


def test_hybrid_falls_back_on_laya_error() -> None:
    fallback = StubFallback(_fallback_result())
    provider = HybridUnderstandingProvider(
        laya=LayaUnderstandingProvider(
            StubLayaClient(error=LayaError("sidecar unavailable", retryable=True))
        ),
        fallback=fallback,
    )

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天下午三点开会"})
    )

    assert result.reason == "llm fallback"
    assert provider.last_decision_source == "llm"
    assert provider.last_fallback_reason in {"laya_error", "laya_unavailable"}
    assert provider.last_call_count == 2


# -- Jev / System One ------------------------------------------------------


def _jev_response(
    label: str,
    probabilities: dict[str, float],
    *,
    confidence: float | None = None,
) -> dict:
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "intent": {
                "type": "choice",
                "choice": label,
                "probabilities": probabilities,
                "confidence": (
                    probabilities[label] if confidence is None else confidence
                ),
            }
        },
        "usage": {"input_tokens": 539, "output_tokens": 95},
        "provider": "TypeSafe",
    }


def test_jev_uses_cloud_confidence_field_and_reports_model() -> None:
    client = StubLayaClient(
        _jev_response(
            "todo.create",
            {"todo.create": 0.91, "non_task": 0.05},
            confidence=0.83,
        )
    )
    provider = JevUnderstandingProvider(client, min_confidence=0.80)
    task = ChatIngress().create_task("user-1", {"text": "明天下午三点跟张三开评审会"})

    result = provider.understand(task)

    assert provider.name == "jev"
    assert result.intent_candidates[0].name == "todo.create"
    # Jev's confidence is a distribution statistic, not the top probability.
    assert result.intent_candidates[0].confidence == pytest.approx(0.83)
    assert provider.last_model == "typesafe/jev-1.13-20260917"


def test_jev_offers_the_full_contract_option_set() -> None:
    client = StubLayaClient(
        _jev_response("todo.create", {"todo.create": 0.95, "non_task": 0.05})
    )
    provider = JevUnderstandingProvider(client)

    provider.understand(ChatIngress().create_task("user-1", {"text": "明天开会"}))

    criteria = client.questions[0]["intent"]["criteria"]
    # The option set and its order are the model's label space: a fine-tuned
    # Laya head indexes into it positionally, so it is never filtered.
    assert list(criteria) == list(INTENT_OPTION_ORDER)


def test_response_without_any_confidence_field_is_rejected() -> None:
    response = _jev_response("todo.create", {"todo.create": 0.95, "non_task": 0.05})
    del response["answers"]["intent"]["confidence"]
    provider = JevUnderstandingProvider(StubLayaClient(response))

    with pytest.raises(LayaError):
        provider.understand(ChatIngress().create_task("user-1", {"text": "明天开会"}))


class RetryStubHttp:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_SYSTEM_ONE_OK = HttpResult(
    200,
    {},
    b'{"answers":{"intent":{"choice":"todo.create",'
    b'"probabilities":{"todo.create":1.0},"answer_confidence":1.0}}}',
)


def test_system_one_client_retries_transient_failures() -> None:
    http = RetryStubHttp(
        [
            IntegrationError("rate limited", status=429, retryable=True),
            _SYSTEM_ONE_OK,
        ]
    )
    sleeps: list[float] = []
    client = HttpSystemOneClient(
        base_url="https://api.example.test/v1",
        http=http,
        max_retries=1,
        sleep=sleeps.append,
    )

    result = client.predict({"text": "明天开会"}, {"intent": {"type": "choice"}})

    assert result["answers"]["intent"]["choice"] == "todo.create"
    assert len(http.calls) == 2
    assert sleeps == [0.5]


def test_system_one_client_does_not_retry_payment_errors() -> None:
    http = RetryStubHttp(
        [IntegrationError("payment required", status=402, retryable=False)]
    )
    client = HttpSystemOneClient(
        base_url="https://api.example.test/v1", http=http, max_retries=2
    )

    with pytest.raises(LayaError) as excinfo:
        client.predict({"text": "明天开会"}, {"intent": {"type": "choice"}})

    assert excinfo.value.classification == "permanent_error"
    assert len(http.calls) == 1


def test_system_one_client_honours_retry_after() -> None:
    http = RetryStubHttp(
        [
            IntegrationError(
                "rate limited", status=429, retryable=True, retry_after=2.0
            ),
            _SYSTEM_ONE_OK,
        ]
    )
    sleeps: list[float] = []
    client = HttpSystemOneClient(
        base_url="https://api.example.test/v1",
        http=http,
        max_retries=1,
        sleep=sleeps.append,
    )

    client.predict({"text": "明天开会"}, {"intent": {"type": "choice"}})

    assert sleeps == [2.0]


def test_hybrid_can_use_jev_as_primary() -> None:
    fallback = StubFallback(_fallback_result())
    primary = JevUnderstandingProvider(
        StubLayaClient(
            _jev_response("todo.create", {"todo.create": 0.95, "non_task": 0.05})
        )
    )
    provider = HybridUnderstandingProvider(primary=primary, fallback=fallback)

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天下午三点开会"})
    )

    assert result.intent_candidates[0].name == "todo.create"
    assert provider.last_decision_source == "jev"
    assert provider.last_primary_confidence == pytest.approx(0.95)
    assert provider.laya is provider.primary
    assert fallback.calls == 0


def test_container_builds_jev_provider() -> None:
    settings = make_settings(
        understanding_provider="jev",
        jev_base_url="https://api.inferera.com/v1",
        jev_api_key="test-key",
        jev_model="jev-1.13",
    )

    provider = build_understanding_provider(settings)

    assert provider.name == "jev"
    assert provider.model == "jev:jev-1.13"


def test_container_hybrid_can_use_jev_primary() -> None:
    settings = make_settings(
        understanding_provider="hybrid",
        understanding_primary="jev",
        jev_base_url="https://api.inferera.com/v1",
        llm_base_url="http://127.0.0.1:9000/v1",
        llm_model="stub",
    )

    provider = build_understanding_provider(settings)

    assert provider.name == "hybrid"
    assert provider.primary_name == "jev"
    assert provider.model == "jev:jev-1.13+stub"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8110", "http://127.0.0.1:8110/v1/systemone"),
        ("https://api.inferera.com/v1", "https://api.inferera.com/v1/systemone"),
        ("https://api.inferera.com/v1/", "https://api.inferera.com/v1/systemone"),
    ],
)
def test_system_one_url_accepts_both_base_conventions(
    base_url: str, expected: str
) -> None:
    assert systemone_url(base_url) == expected
