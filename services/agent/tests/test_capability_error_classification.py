from app.capabilities.knowledge import KnowledgeToolUnavailable
from app.capabilities.person import PersonQueryUnavailable
from app.capabilities.person_scope import PersonScopeUnavailable
from app.capabilities.report import WeeklyReportUnavailable
from app.kernel.errors import classify_error


def test_transient_capability_failures_are_retryable() -> None:
    errors = [
        KnowledgeToolUnavailable("knowledge unavailable"),
        PersonQueryUnavailable("person lookup is unavailable"),
        PersonScopeUnavailable("person scope is unavailable"),
        WeeklyReportUnavailable("report lookup is unavailable"),
    ]

    assert [classify_error(error) for error in errors] == [
        "retryable_error",
        "retryable_error",
        "retryable_error",
        "retryable_error",
    ]
