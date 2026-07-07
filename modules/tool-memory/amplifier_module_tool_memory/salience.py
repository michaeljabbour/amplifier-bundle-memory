"""T1-GATE-1 — the salience gate.

A pure, side-effect-free decision function that decides whether a candidate
plasticity write is worth performing. Composed of three signals:

    salience = novelty x reward x surprise   (each in [0.0, 1.0])

The product is intentionally multiplicative: if *any* component is ~0 the
write is rejected. This encodes the constellation's prime directive —
**default: don't learn**. A write only happens when novelty, reward, and
surprise *all* clear the bar together.

This module has NO side effects, performs NO I/O, and imports nothing from
the rest of the bundle. It MAY be imported by the behavioral write hook
(co-mounted by memory) or by the conductor (behavioral-plasticity) bundle.
It is the only piece of the salience decision and is unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SalienceInput",
    "SalienceConfig",
    "SalienceDecision",
    "evaluate_salience",
]


def _clamp(x: float) -> float:
    """Clamp an arbitrary number into the unit interval [0.0, 1.0]."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


@dataclass(frozen=True)
class SalienceInput:
    """The three salience signals for one candidate write.

    novelty : how new is this content relative to what is already stored
              (0 = exact duplicate, 1 = never seen before).
    reward  : the value of the outcome the write is reacting to
              (0 = worthless, 1 = strongly rewarding/instructive).
    surprise: prediction error — how far the outcome diverged from expectation
              (0 = fully expected, 1 = maximally surprising).
    """

    novelty: float
    reward: float
    surprise: float


@dataclass(frozen=True)
class SalienceConfig:
    """Thresholds controlling the gate. Defaults REJECT in the neutral case.

    ``threshold`` is compared against the composite product. With the default
    of 0.6 a neutral candidate (0.5, 0.5, 0.5 -> 0.125) is rejected; each
    component must average ~0.84 to pass. Per-signal floors default to 0.0
    (disabled) and provide independent hard gates when configured.
    """

    threshold: float = 0.6
    min_novelty: float = 0.0
    min_reward: float = 0.0
    min_surprise: float = 0.0
    # Optional per-signal exponents (weights). Default 1.0 -> plain product.
    weights: tuple[float, float, float] = field(default=(1.0, 1.0, 1.0))


DEFAULT_CONFIG = SalienceConfig()


@dataclass(frozen=True)
class SalienceDecision:
    """The gate's verdict. ``write`` is the only thing callers must honour."""

    write: bool
    salience: float
    reason: str
    components: dict[str, float]


def evaluate_salience(
    signals: SalienceInput,
    config: SalienceConfig = DEFAULT_CONFIG,
) -> SalienceDecision:
    """Decide whether a candidate write clears the salience bar.

    Pure: depends only on its arguments, returns a value, mutates nothing.
    Default behaviour REJECTS unless the multiplicative salience exceeds
    ``config.threshold`` and every per-signal floor is satisfied.
    """
    novelty = _clamp(signals.novelty)
    reward = _clamp(signals.reward)
    surprise = _clamp(signals.surprise)
    components = {"novelty": novelty, "reward": reward, "surprise": surprise}

    # Per-signal hard floors (independent gates; default 0.0 = disabled).
    if novelty < config.min_novelty:
        return SalienceDecision(False, 0.0, "novelty_below_floor", components)
    if reward < config.min_reward:
        return SalienceDecision(False, 0.0, "reward_below_floor", components)
    if surprise < config.min_surprise:
        return SalienceDecision(False, 0.0, "surprise_below_floor", components)

    wn, wr, ws = config.weights
    salience = round((novelty**wn) * (reward**wr) * (surprise**ws), 10)

    if salience > config.threshold:
        return SalienceDecision(True, salience, "cleared", components)
    return SalienceDecision(False, salience, "below_threshold", components)
