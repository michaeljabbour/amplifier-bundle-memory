"""T1-GATE-1 — unit tests for the pure salience gate.

The gate must DEFAULT TO REJECT and only pass when novelty x reward x surprise
clears the configured bar. It must be pure (no I/O, no mutation).
"""

from __future__ import annotations

from amplifier_module_tool_mempalace.salience import (
    SalienceConfig,
    SalienceInput,
    evaluate_salience,
)


def test_neutral_candidate_is_rejected_by_default() -> None:
    """0.5 x 0.5 x 0.5 = 0.125 < 0.6 default threshold -> no write."""
    d = evaluate_salience(SalienceInput(0.5, 0.5, 0.5))
    assert d.write is False
    assert d.reason == "below_threshold"
    assert abs(d.salience - 0.125) < 1e-9


def test_all_high_signals_clear_the_bar() -> None:
    d = evaluate_salience(SalienceInput(0.9, 0.9, 0.9))
    assert d.write is True
    assert d.reason == "cleared"
    assert d.salience > 0.6


def test_any_zero_component_kills_the_write() -> None:
    """Multiplicative: a single zeroed signal forces rejection."""
    for sig in (
        SalienceInput(0.0, 1.0, 1.0),
        SalienceInput(1.0, 0.0, 1.0),
        SalienceInput(1.0, 1.0, 0.0),
    ):
        assert evaluate_salience(sig).write is False


def test_empty_signals_reject() -> None:
    d = evaluate_salience(SalienceInput(0.0, 0.0, 0.0))
    assert d.write is False
    assert d.salience == 0.0


def test_threshold_is_strict_greater_than() -> None:
    """salience exactly == threshold does NOT write (default-reject bias)."""
    cfg = SalienceConfig(threshold=0.125)
    d = evaluate_salience(SalienceInput(0.5, 0.5, 0.5), cfg)
    assert abs(d.salience - 0.125) < 1e-9
    assert d.write is False


def test_per_signal_floor_gates_independently() -> None:
    cfg = SalienceConfig(threshold=0.0, min_novelty=0.5)
    # Would clear threshold (0.0) but novelty floor rejects.
    d = evaluate_salience(SalienceInput(0.1, 1.0, 1.0), cfg)
    assert d.write is False
    assert d.reason == "novelty_below_floor"


def test_inputs_are_clamped_not_exploded() -> None:
    """Out-of-range and NaN inputs are clamped into [0,1], never crash."""
    d = evaluate_salience(SalienceInput(5.0, 2.0, 1.0))  # all clamp to 1.0
    assert d.write is True
    assert d.components == {"novelty": 1.0, "reward": 1.0, "surprise": 1.0}
    d2 = evaluate_salience(SalienceInput(float("nan"), 1.0, 1.0))
    assert d2.components["novelty"] == 0.0
    assert d2.write is False


def test_gate_is_pure_no_mutation_of_input() -> None:
    sig = SalienceInput(0.9, 0.9, 0.9)
    before = (sig.novelty, sig.reward, sig.surprise)
    evaluate_salience(sig)
    assert (sig.novelty, sig.reward, sig.surprise) == before
