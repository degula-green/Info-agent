"""/metrics accepts two header shapes, because Prometheus cannot send custom ones."""

from app.routers.health import _presented_token


def test_the_internal_header_is_used_when_present():
    assert _presented_token("secret", None) == "secret"


def test_a_bearer_token_is_accepted():
    # Prometheus scrape configs have no free-form header field; Bearer is the
    # only shape they can send.
    assert _presented_token(None, "Bearer secret") == "secret"


def test_bearer_prefix_is_case_insensitive():
    assert _presented_token(None, "bearer secret") == "secret"
    assert _presented_token(None, "BEARER secret") == "secret"


def test_the_internal_header_wins_when_both_are_sent():
    assert _presented_token("from-header", "Bearer from-bearer") == "from-header"


def test_an_empty_bearer_is_not_a_token():
    assert _presented_token(None, "Bearer ") is None
    assert _presented_token(None, "Bearer") is None


def test_a_non_bearer_authorization_is_ignored():
    assert _presented_token(None, "Basic dXNlcjpwYXNz") is None


def test_no_headers_means_no_token():
    assert _presented_token(None, None) is None
    assert _presented_token("", "") is None
