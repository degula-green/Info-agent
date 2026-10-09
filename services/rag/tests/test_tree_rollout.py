"""Rollout control: default mode, scope whitelist, and shadow sampling."""

from app.application.tree_rollout import (
    parse_rollout_scopes,
    resolve_tree_mode,
    shadow_sampled,
)

SCOPE = dict(scope_type="user", scope_id="u-1")


def test_whitelisted_scope_is_promoted_to_tree():
    mode = resolve_tree_mode(
        default="shadow",
        rollout_scopes=parse_rollout_scopes("user:u-1"),
        **SCOPE,
    )

    assert mode == "tree"


def test_scope_outside_the_whitelist_keeps_the_default():
    mode = resolve_tree_mode(
        default="shadow",
        scope_type="user",
        scope_id="u-2",
        rollout_scopes=parse_rollout_scopes("user:u-1"),
    )

    assert mode == "shadow"


def test_off_is_a_kill_switch_that_outranks_the_whitelist():
    # The rollback plan is "shadow, then off". A whitelist that outranked off
    # would leave the scopes you were trying to protect still on tree mode.
    mode = resolve_tree_mode(
        default="off",
        rollout_scopes=parse_rollout_scopes("user:u-1"),
        **SCOPE,
    )

    assert mode == "off"


def test_org_and_user_scopes_are_distinguished():
    whitelist = parse_rollout_scopes("organization:o-1")

    assert resolve_tree_mode(
        default="shadow", scope_type="organization", scope_id="o-1",
        rollout_scopes=whitelist,
    ) == "tree"
    # Same id, different scope type: not the same tenant.
    assert resolve_tree_mode(
        default="shadow", scope_type="user", scope_id="o-1",
        rollout_scopes=whitelist,
    ) == "shadow"


def test_global_tree_mode_needs_no_whitelist():
    assert resolve_tree_mode(
        default="tree", scope_type="user", scope_id="any",
    ) == "tree"


def test_unknown_default_falls_back_to_off():
    assert resolve_tree_mode(
        default="boost", scope_type="user", scope_id="any",
    ) == "off"


def test_rollout_scope_parsing_ignores_noise():
    parsed = parse_rollout_scopes(
        " user:u-1 , organization:o-1 ;broken, ,user:u-1"
    )

    assert parsed == frozenset({"user:u-1", "organization:o-1"})
    assert parse_rollout_scopes("") == frozenset()
    assert parse_rollout_scopes(None) == frozenset()


def test_full_rate_samples_everything_and_zero_samples_nothing():
    assert shadow_sampled(**SCOPE, query="q", sample_rate=1.0) is True
    assert shadow_sampled(**SCOPE, query="q", sample_rate=0.0) is False


def test_sampling_is_deterministic_for_a_retried_query():
    # A user retrying the same question must not flip between sampled and not,
    # otherwise the diagnostics for one query disagree with each other.
    first = shadow_sampled(**SCOPE, query="青云项目进展", sample_rate=0.3)
    second = shadow_sampled(**SCOPE, query="青云项目进展", sample_rate=0.3)

    assert first == second


def test_sampling_rate_is_roughly_respected_across_many_queries():
    sampled = [
        shadow_sampled(**SCOPE, query=f"query-{index}", sample_rate=0.25)
        for index in range(2000)
    ]

    ratio = sum(sampled) / len(sampled)
    assert 0.20 < ratio < 0.30


def test_sampling_is_stable_across_processes():
    # sha256, not Python's salted hash(): the same query must sample the same
    # way after a worker restart.
    assert shadow_sampled(**SCOPE, query="青云项目进展", sample_rate=0.5) is True
