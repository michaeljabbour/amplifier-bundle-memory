"""T1-MEM-3 — usage weighting + decay (pure functions).

Two tertiary signals that nudge the importance/rerank score:

* **usage weighting** — drawers that are retrieved often are slightly more
  useful. The boost is *bounded* and *saturating* so heavy reuse can never
  dominate semantic similarity (the prime recall guarantee).
* **decay** — importance is not forever. A stale high-importance drawer relaxes
  back toward the neutral midpoint over time, shrinking its rerank influence —
  but it never inverts below neutral.

Both are pure: no I/O, no mutation. The *source* of ``retrieval_count`` /
``last_accessed`` is amplifier-data's existing access-count fold (AccessEvents
via ``record_access``); we consume it here rather than re-implementing counting.
"""

from __future__ import annotations

__all__ = [
    "NEUTRAL_IMPORTANCE",
    "USAGE_SCALE",
    "usage_adjustment",
    "decay_factor",
    "decay_importance",
]

#: The zero-boost midpoint shared with the briefing reranker. Decay relaxes
#: toward this value; usage boosts are added on top of the importance term.
NEUTRAL_IMPORTANCE = 0.5

#: Usage is a *tertiary* signal: its max contribution is intentionally smaller
#: than the importance term's scale (0.08), so it cannot reorder semantically
#: distinct results on its own.
USAGE_SCALE = 0.04


def usage_adjustment(
    retrieval_count: int | None,
    *,
    weight: float = 1.0,
    saturation: float = 10.0,
) -> float:
    """Bounded, saturating boost from how often a drawer has been retrieved.

    Returns a value in ``[0, weight * USAGE_SCALE]``. The boost saturates at
    ``saturation`` retrievals, so a drawer accessed 1000 times gets the same
    cap as one accessed ``saturation`` times — usage informs, never dominates.
    """
    rc = retrieval_count or 0
    if rc <= 0 or weight <= 0.0 or saturation <= 0.0:
        return 0.0
    frac = min(1.0, float(rc) / float(saturation))
    return round(weight * USAGE_SCALE * frac, 10)


def decay_factor(age_days: float, *, half_life_days: float = 30.0) -> float:
    """Multiplicative decay in ``(0, 1]``: 1.0 when fresh, 0.5 at one half-life.

    Negative ages (clock skew) clamp to 1.0 (treated as fresh).
    """
    if half_life_days <= 0.0:
        return 1.0
    if age_days <= 0.0:
        return 1.0
    return round(0.5 ** (float(age_days) / float(half_life_days)), 10)


def decay_importance(
    importance: float,
    *,
    age_days: float,
    half_life_days: float = 30.0,
    floor: float = NEUTRAL_IMPORTANCE,
) -> float:
    """Relax a stale importance toward the neutral ``floor`` over time.

    ``age_days = 0`` returns the value unchanged; as age grows the value moves
    geometrically toward ``floor`` (the zero-boost midpoint) but never crosses
    it. This shrinks a stale drawer's rerank influence without inverting its
    sign.
    """
    f = decay_factor(age_days, half_life_days=half_life_days)
    return round(floor + (float(importance) - floor) * f, 10)
