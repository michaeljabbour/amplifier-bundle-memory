"""
Phase 3 — Palace intelligence: KG enrichment helpers.

Pure functions used by the Curator agent at session:end to produce the set
of knowledge-graph facts that should be added for each drawer filed during
the session. Extracted from agent instructions for determinism and testability.

Public API:
    compute_importance(category, signals) -> float
    duplicate_action(score)              -> "duplicates" | "related_to" | None
    plan_phase3_actions(drawers)         -> list[KGFact]

See agents/curator.md Phase 3 for how these are used in practice.
Spec: docs/spec-v1.2.0-gene-transfer.md Section 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawerRecord:
    """Input record describing one drawer filed during Phase 1.

    Attributes:
        id:        Drawer identifier (used as ``drawer:<id>`` in KG subjects).
        category:  Detected category string, or None if uncategorized.
        signals:   Dict of boolean signal flags for the importance rubric.
                   All keys default to False when absent.  Known keys:
                   architecture_level, user_explicit, cross_wing, unresolved,
                   root_cause_documented, external_breaking, cross_project,
                   gotcha_adjacent.
        dup_match: (matching_drawer_id, cosine_score) if a duplicate was
                   found by mempalace_check_duplicate in Phase 1, else None.
    """

    id: str
    category: str | None
    signals: dict[str, bool]
    dup_match: tuple[str, float] | None


@dataclass(frozen=True)
class KGFact:
    """A single knowledge-graph triple to be pushed via mempalace_kg_add.

    Attributes:
        subject:   e.g. ``"drawer:abc123"``
        predicate: one of ``has_importance``, ``has_category``,
                   ``duplicates``, ``related_to``
        object:    string value (importance score, category name, or drawer id)
    """

    subject: str
    predicate: str
    object: str


# ---------------------------------------------------------------------------
# Importance rubric
# ---------------------------------------------------------------------------

# (base_score, {signal_key: boost_amount}, cap)
_RUBRIC: dict[str | None, tuple[float, dict[str, float], float]] = {
    "decision": (
        0.75,
        {"architecture_level": 0.10, "user_explicit": 0.15},
        1.0,
    ),
    "architecture": (
        0.70,
        {"cross_wing": 0.10},
        1.0,
    ),
    "blocker": (
        0.65,
        {"unresolved": 0.10},
        0.9,
    ),
    "resolved_blocker": (
        0.55,
        {"root_cause_documented": 0.10},
        0.8,
    ),
    "dependency": (
        0.50,
        {"external_breaking": 0.10},
        0.8,
    ),
    "pattern": (
        0.50,
        {"cross_project": 0.10},
        0.8,
    ),
    "lesson_learned": (
        0.45,
        {"gotcha_adjacent": 0.10},
        0.7,
    ),
    # Sentinel for uncategorized — also used as the default
    None: (0.40, {}, 0.5),
}

# Importance override for near-identical duplicates (score ≥ 0.95)
_DUPLICATE_IMPORTANCE_OVERRIDE = 0.15

# Cosine thresholds for duplicate actions
_NEAR_IDENTICAL_THRESHOLD = 0.95
_RELATED_THRESHOLD = 0.85


def compute_importance(category: str | None, signals: dict[str, bool]) -> float:
    """Return the importance score for a drawer based on its category and signals.

    Applies the rubric: base score + per-signal boosts, capped at the
    category maximum.  Unknown category strings fall back to the uncategorized
    defaults (base 0.40, no boosts, cap 0.50).

    Args:
        category: Detected category string, or None for uncategorized.
        signals:  Dict of boolean flags; absent keys are treated as False.

    Returns:
        Importance score in [0.0, 1.0].
    """
    base, boosts, cap = _RUBRIC.get(category, _RUBRIC[None])
    score = base + sum(
        amount for key, amount in boosts.items() if signals.get(key, False)
    )
    return round(min(score, cap), 10)  # round to avoid float drift


def duplicate_action(
    score: float,
) -> Literal["duplicates", "related_to"] | None:
    """Determine the KG relationship action for a given duplicate cosine score.

    Returns:
        ``"duplicates"``  if score ≥ 0.95
        ``"related_to"``  if 0.85 ≤ score < 0.95
        ``None``          if score < 0.85
    """
    if score >= _NEAR_IDENTICAL_THRESHOLD:
        return "duplicates"
    if score >= _RELATED_THRESHOLD:
        return "related_to"
    return None


# ---------------------------------------------------------------------------
# Action plan
# ---------------------------------------------------------------------------


def plan_phase3_actions(drawers: list[DrawerRecord]) -> list[KGFact]:
    """Produce the complete set of KG facts to add for a batch of drawers.

    For each DrawerRecord:
    1. Determine if a near-identical duplicate override applies (importance → 0.15).
    2. Emit ``(drawer:<id>, has_importance, <score>)``.
    3. Emit ``(drawer:<id>, has_category, <category>)`` if category is not None.
    4. If dup_match exists and action is not None:
       - Emit the duplicate or related_to edge.

    This function is pure — it produces the same output for identical input and
    has no side effects. The Curator agent is responsible for filtering already-
    present triples (idempotency check) before pushing these to MemPalace.

    Args:
        drawers: List of DrawerRecord objects describing the Phase 1 filing batch.

    Returns:
        Ordered list of KGFact objects ready to pass to mempalace_kg_add.
    """
    facts: list[KGFact] = []

    for drawer in drawers:
        subject = f"drawer:{drawer.id}"

        # Determine duplicate relationship (if any)
        action: Literal["duplicates", "related_to"] | None = None
        is_near_identical = False
        if drawer.dup_match is not None:
            match_id, dup_score = drawer.dup_match
            action = duplicate_action(dup_score)
            is_near_identical = action == "duplicates"

        # Compute importance (with override for near-identical duplicates)
        if is_near_identical:
            importance = _DUPLICATE_IMPORTANCE_OVERRIDE
        else:
            importance = compute_importance(drawer.category, drawer.signals)

        # Always emit importance fact
        facts.append(
            KGFact(subject=subject, predicate="has_importance", object=str(importance))
        )

        # Emit category fact (if categorized)
        if drawer.category is not None:
            facts.append(
                KGFact(
                    subject=subject, predicate="has_category", object=drawer.category
                )
            )

        # Emit duplicate/related edge (if applicable)
        if action is not None and drawer.dup_match is not None:
            match_id, _ = drawer.dup_match
            facts.append(
                KGFact(
                    subject=subject,
                    predicate=action,
                    object=f"drawer:{match_id}",
                )
            )

    return facts
