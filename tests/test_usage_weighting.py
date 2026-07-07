"""T1-MEM-3 — usage weighting + decay.

Verifies the pure usage/decay functions are bounded and monotonic, and that
wiring usage into the briefing reranker is a no-op by default (R@5 preserved)
and can never let usage dominate semantic similarity when enabled.
"""

from __future__ import annotations

from amplifier_module_tool_memory.usage import (
    USAGE_SCALE,
    decay_factor,
    decay_importance,
    usage_adjustment,
)


def test_usage_is_bounded_and_saturates() -> None:
    assert usage_adjustment(0) == 0.0
    assert usage_adjustment(None) == 0.0
    # Monotonic up to saturation, then capped.
    a1 = usage_adjustment(1, saturation=10.0)
    a5 = usage_adjustment(5, saturation=10.0)
    a10 = usage_adjustment(10, saturation=10.0)
    a1000 = usage_adjustment(1000, saturation=10.0)
    assert 0.0 < a1 < a5 < a10
    assert a10 == a1000  # saturated
    assert a10 == USAGE_SCALE  # weight=1.0 cap


def test_usage_weight_zero_disables() -> None:
    assert usage_adjustment(100, weight=0.0) == 0.0


def test_decay_factor_halves_at_half_life() -> None:
    assert decay_factor(0.0) == 1.0
    assert decay_factor(-5.0) == 1.0  # clock skew clamps to fresh
    assert abs(decay_factor(30.0, half_life_days=30.0) - 0.5) < 1e-9
    assert abs(decay_factor(60.0, half_life_days=30.0) - 0.25) < 1e-9


def test_decay_relaxes_toward_neutral_never_inverts() -> None:
    # A stale high importance decays DOWN toward 0.5 but never below it.
    fresh = decay_importance(0.9, age_days=0.0)
    aged = decay_importance(0.9, age_days=30.0, half_life_days=30.0)
    very_old = decay_importance(0.9, age_days=3000.0, half_life_days=30.0)
    assert fresh == 0.9
    assert 0.5 < aged < 0.9
    assert abs(very_old - 0.5) < 1e-6
    # A low importance decays UP toward 0.5, never above it.
    low_aged = decay_importance(0.1, age_days=30.0, half_life_days=30.0)
    assert 0.1 < low_aged < 0.5


def test_rerank_usage_off_by_default_is_unchanged() -> None:
    from amplifier_module_hooks_memory_briefing import _rerank_by_importance

    results = [
        {"id": "a", "score": 0.90},
        {"id": "b", "score": 0.80},
        {"id": "c", "score": 0.70},
    ]
    importance = {"a": 0.5, "b": 0.5, "c": 0.5}
    # No usage args -> identical to importance-only path (here, semantic order).
    out = _rerank_by_importance(results, importance, weight=1.0)
    assert [r["id"] for r in out] == ["a", "b", "c"]


def test_rerank_usage_cannot_dominate_semantic() -> None:
    """A heavily-used low-semantic result must NOT overtake a clearly better
    semantic result. Usage is a tie-breaker-scale nudge, not a lever."""
    from amplifier_module_hooks_memory_briefing import _rerank_by_importance

    results = [
        {"id": "strong", "score": 0.90},
        {"id": "weak", "score": 0.80},
    ]
    importance = {"strong": 0.5, "weak": 0.5}
    usage = {"weak": 100000}  # massively used
    out = _rerank_by_importance(
        results, importance, weight=1.0, usage_lookup=usage, usage_weight=1.0
    )
    # 0.10 semantic gap >> max usage nudge (USAGE_SCALE=0.04); strong stays first.
    assert out[0]["id"] == "strong"


def test_rerank_usage_breaks_ties_when_enabled() -> None:
    from amplifier_module_hooks_memory_briefing import _rerank_by_importance

    results = [
        {"id": "x", "score": 0.80},
        {"id": "y", "score": 0.80},
    ]
    importance = {"x": 0.5, "y": 0.5}
    usage = {"y": 50}  # y used more, equal semantics -> y rises
    out = _rerank_by_importance(
        results, importance, weight=1.0, usage_lookup=usage, usage_weight=1.0
    )
    assert out[0]["id"] == "y"
