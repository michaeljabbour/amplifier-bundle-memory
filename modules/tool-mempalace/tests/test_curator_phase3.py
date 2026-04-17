"""
Tests for amplifier_module_tool_mempalace.phase3

Section 8.4 of spec-v1.2.0-gene-transfer.md

These are pure unit tests — no MCP calls, no palace fixture.
The integration tests at the bottom are gated behind mempalace CLI availability.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict

import pytest

from amplifier_module_tool_mempalace.phase3 import (
    DrawerRecord,
    KGFact,
    compute_importance,
    duplicate_action,
    plan_phase3_actions,
)


# ---------------------------------------------------------------------------
# compute_importance — base scores (no signals)
# ---------------------------------------------------------------------------


class TestComputeImportanceBase:
    def test_compute_importance_uncategorized(self) -> None:
        assert compute_importance(None, {}) == 0.40

    def test_compute_importance_decision(self) -> None:
        assert compute_importance("decision", {}) == 0.75

    def test_compute_importance_architecture(self) -> None:
        assert compute_importance("architecture", {}) == 0.70

    def test_compute_importance_blocker(self) -> None:
        assert compute_importance("blocker", {}) == 0.65

    def test_compute_importance_resolved_blocker(self) -> None:
        assert compute_importance("resolved_blocker", {}) == 0.55

    def test_compute_importance_dependency(self) -> None:
        assert compute_importance("dependency", {}) == 0.50

    def test_compute_importance_pattern(self) -> None:
        assert compute_importance("pattern", {}) == 0.50

    def test_compute_importance_lesson_learned(self) -> None:
        assert compute_importance("lesson_learned", {}) == 0.45

    def test_compute_importance_unknown_category(self) -> None:
        # Unknown category falls back to uncategorized base
        assert compute_importance("unknown_xyz", {}) == 0.40


# ---------------------------------------------------------------------------
# compute_importance — boosts
# ---------------------------------------------------------------------------


class TestComputeImportanceBoosts:
    def test_decision_architecture_level_boost(self) -> None:
        score = compute_importance("decision", {"architecture_level": True})
        assert score == pytest.approx(0.85)  # 0.75 + 0.10

    def test_decision_user_explicit_boost(self) -> None:
        score = compute_importance("decision", {"user_explicit": True})
        assert score == pytest.approx(0.90)  # 0.75 + 0.15

    def test_decision_all_boosts_capped(self) -> None:
        # 0.75 + 0.10 + 0.15 = 1.00, cap = 1.0
        score = compute_importance(
            "decision", {"architecture_level": True, "user_explicit": True}
        )
        assert score == pytest.approx(1.0)

    def test_architecture_cross_wing_boost(self) -> None:
        score = compute_importance("architecture", {"cross_wing": True})
        assert score == pytest.approx(0.80)

    def test_blocker_unresolved_boost(self) -> None:
        score = compute_importance("blocker", {"unresolved": True})
        assert score == pytest.approx(0.75)

    def test_resolved_blocker_root_cause_boost(self) -> None:
        score = compute_importance("resolved_blocker", {"root_cause_documented": True})
        assert score == pytest.approx(0.65)

    def test_dependency_external_breaking_boost(self) -> None:
        score = compute_importance("dependency", {"external_breaking": True})
        assert score == pytest.approx(0.60)

    def test_pattern_cross_project_boost(self) -> None:
        score = compute_importance("pattern", {"cross_project": True})
        assert score == pytest.approx(0.60)

    def test_lesson_learned_gotcha_adjacent_boost(self) -> None:
        score = compute_importance("lesson_learned", {"gotcha_adjacent": True})
        assert score == pytest.approx(0.55)

    def test_uncategorized_no_boost_capped(self) -> None:
        # Uncategorized has no boosts, cap=0.5
        score = compute_importance(None, {"architecture_level": True})
        assert score == pytest.approx(0.40)  # signals ignored for uncategorized

    def test_compute_importance_all_boosts_decision(self) -> None:
        # decision + architecture_level + user_explicit = min(1.0, 1.0) = 1.0
        score = compute_importance(
            "decision", {"architecture_level": True, "user_explicit": True}
        )
        assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_importance — cap enforcement
# ---------------------------------------------------------------------------


class TestComputeImportanceCaps:
    def test_blocker_cap_at_0_9(self) -> None:
        # blocker base=0.65 + unresolved boost=0.10 = 0.75, cap=0.9 → 0.75
        # Even with extra irrelevant signals, cap is not exceeded
        score = compute_importance("blocker", {"unresolved": True})
        assert score <= 0.9

    def test_lesson_learned_cap_at_0_7(self) -> None:
        score = compute_importance("lesson_learned", {"gotcha_adjacent": True})
        assert score <= 0.7

    def test_uncategorized_cap_at_0_5(self) -> None:
        score = compute_importance(None, {})
        assert score <= 0.5

    def test_score_always_positive(self) -> None:
        for cat in [
            "decision",
            "architecture",
            "blocker",
            "resolved_blocker",
            "dependency",
            "pattern",
            "lesson_learned",
            None,
        ]:
            assert compute_importance(cat, {}) > 0


# ---------------------------------------------------------------------------
# duplicate_action — threshold boundaries
# ---------------------------------------------------------------------------


class TestDuplicateAction:
    def test_duplicate_action_boundaries(self) -> None:
        assert duplicate_action(0.95) == "duplicates"
        assert duplicate_action(1.00) == "duplicates"
        assert duplicate_action(0.85) == "related_to"
        assert duplicate_action(0.94) == "related_to"
        assert duplicate_action(0.84) is None
        assert duplicate_action(0.00) is None

    def test_exactly_0_95(self) -> None:
        assert duplicate_action(0.95) == "duplicates"

    def test_exactly_0_85(self) -> None:
        assert duplicate_action(0.85) == "related_to"

    def test_exactly_0_84(self) -> None:
        assert duplicate_action(0.84) is None


# ---------------------------------------------------------------------------
# plan_phase3_actions — duplicate linking
# ---------------------------------------------------------------------------


class TestPlanPhase3DuplicateLinking:
    def test_duplicate_linking_high(self) -> None:
        """Two drawers at 0.96 similarity → duplicates KGFact + has_importance=0.15 on newer."""
        drawers = [
            DrawerRecord(
                id="drawer_new",
                category="decision",
                signals={},
                dup_match=("drawer_existing", 0.96),
            ),
        ]
        facts = plan_phase3_actions(drawers)

        predicates = {f.predicate: f for f in facts}
        # Must have duplicates edge
        assert "duplicates" in predicates, f"Missing 'duplicates' fact in {facts}"
        dup_fact = predicates["duplicates"]
        assert dup_fact.subject == "drawer:drawer_new"
        assert dup_fact.object == "drawer:drawer_existing"

        # has_importance must be 0.15 (override, not the decision base of 0.75)
        assert "has_importance" in predicates
        assert float(predicates["has_importance"].object) == pytest.approx(0.15)

    def test_duplicate_linking_medium(self) -> None:
        """Two drawers at 0.88 similarity → related_to KGFact, no importance override."""
        drawers = [
            DrawerRecord(
                id="drawer_new",
                category="decision",
                signals={},
                dup_match=("drawer_existing", 0.88),
            ),
        ]
        facts = plan_phase3_actions(drawers)
        predicates = {f.predicate: f for f in facts}

        assert "related_to" in predicates, f"Missing 'related_to' in {facts}"
        assert predicates["related_to"].object == "drawer:drawer_existing"

        # No duplicates edge
        assert "duplicates" not in predicates

        # Importance is NOT overridden — should be decision base (0.75)
        assert "has_importance" in predicates
        assert float(predicates["has_importance"].object) == pytest.approx(0.75)

    def test_duplicate_linking_distinct(self) -> None:
        """Drawers at 0.70 similarity → no dup KGFact."""
        drawers = [
            DrawerRecord(
                id="drawer_a",
                category="pattern",
                signals={},
                dup_match=("drawer_b", 0.70),
            ),
        ]
        facts = plan_phase3_actions(drawers)
        predicates = {f.predicate: f for f in facts}

        assert "duplicates" not in predicates
        assert "related_to" not in predicates
        # But importance and category should still be present
        assert "has_importance" in predicates

    def test_no_dup_match(self) -> None:
        """No dup_match → no dup/related edge, normal importance."""
        drawers = [
            DrawerRecord(
                id="d1",
                category="architecture",
                signals={"cross_wing": True},
                dup_match=None,
            ),
        ]
        facts = plan_phase3_actions(drawers)
        predicates = {f.predicate: f for f in facts}

        assert "duplicates" not in predicates
        assert "related_to" not in predicates
        assert float(predicates["has_importance"].object) == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# plan_phase3_actions — importance and category tagging
# ---------------------------------------------------------------------------


class TestPlanPhase3Tagging:
    def test_importance_scoring(self) -> None:
        """Each category with no signals gets its base score."""
        cases = [
            ("decision", 0.75),
            ("architecture", 0.70),
            ("blocker", 0.65),
            ("resolved_blocker", 0.55),
            ("dependency", 0.50),
            ("pattern", 0.50),
            ("lesson_learned", 0.45),
        ]
        for i, (cat, expected) in enumerate(cases):
            drawer = DrawerRecord(id=f"d{i}", category=cat, signals={}, dup_match=None)
            facts = plan_phase3_actions([drawer])
            importance_facts = [f for f in facts if f.predicate == "has_importance"]
            assert len(importance_facts) == 1
            assert float(importance_facts[0].object) == pytest.approx(expected), (
                f"Category '{cat}': expected {expected}, got {importance_facts[0].object}"
            )

    def test_category_tag_present(self) -> None:
        drawers = [
            DrawerRecord(id="d1", category="decision", signals={}, dup_match=None)
        ]
        facts = plan_phase3_actions(drawers)
        cat_facts = [f for f in facts if f.predicate == "has_category"]
        assert len(cat_facts) == 1
        assert cat_facts[0].object == "decision"
        assert cat_facts[0].subject == "drawer:d1"

    def test_no_category_tag_when_uncategorized(self) -> None:
        drawers = [DrawerRecord(id="d1", category=None, signals={}, dup_match=None)]
        facts = plan_phase3_actions(drawers)
        cat_facts = [f for f in facts if f.predicate == "has_category"]
        assert len(cat_facts) == 0

    def test_multiple_drawers_independent(self) -> None:
        """Each drawer gets its own facts; no cross-contamination."""
        drawers = [
            DrawerRecord(id="d1", category="decision", signals={}, dup_match=None),
            DrawerRecord(id="d2", category="pattern", signals={}, dup_match=None),
        ]
        facts = plan_phase3_actions(drawers)
        d1_facts = [f for f in facts if f.subject == "drawer:d1"]
        d2_facts = [f for f in facts if f.subject == "drawer:d2"]
        assert len(d1_facts) == 2  # has_importance + has_category
        assert len(d2_facts) == 2


# ---------------------------------------------------------------------------
# plan_phase3_actions — idempotency
# ---------------------------------------------------------------------------


class TestPlanPhase3Idempotency:
    def test_empty_drawers(self) -> None:
        """plan_phase3_actions on empty input returns empty list."""
        assert plan_phase3_actions([]) == []

    def test_idempotency(self) -> None:
        """Calling plan_phase3_actions twice on identical input produces identical output."""
        drawers = [
            DrawerRecord(
                id="d1",
                category="decision",
                signals={"architecture_level": True},
                dup_match=("d0", 0.92),
            ),
            DrawerRecord(id="d2", category="pattern", signals={}, dup_match=None),
        ]
        result_a = plan_phase3_actions(drawers)
        result_b = plan_phase3_actions(drawers)

        assert len(result_a) == len(result_b)
        for fa, fb in zip(result_a, result_b):
            assert asdict(fa) == asdict(fb)


# ---------------------------------------------------------------------------
# KGFact shape
# ---------------------------------------------------------------------------


class TestKGFactShape:
    def test_kgfact_has_required_fields(self) -> None:
        fact = KGFact(subject="drawer:abc", predicate="has_importance", object="0.75")
        assert fact.subject == "drawer:abc"
        assert fact.predicate == "has_importance"
        assert fact.object == "0.75"

    def test_plan_returns_kgfact_instances(self) -> None:
        drawers = [
            DrawerRecord(id="d1", category="decision", signals={}, dup_match=None)
        ]
        facts = plan_phase3_actions(drawers)
        assert all(isinstance(f, KGFact) for f in facts)


# ---------------------------------------------------------------------------
# Integration tests (skip if mempalace CLI unavailable)
# ---------------------------------------------------------------------------


def _mempalace_available() -> bool:
    try:
        result = subprocess.run(
            ["mempalace", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


MEMPALACE_AVAILABLE = _mempalace_available()
skip_no_mempalace = pytest.mark.skipif(
    not MEMPALACE_AVAILABLE,
    reason="mempalace CLI not available in test environment",
)


@skip_no_mempalace
class TestPhase3IntegrationMCP:
    def test_plan_facts_are_valid_mcp_args(self) -> None:
        """KGFacts produced by plan_phase3_actions are valid MCP kg_add arguments."""
        import json

        drawers = [
            DrawerRecord(
                id="integ_d1",
                category="decision",
                signals={"user_explicit": True},
                dup_match=None,
            )
        ]
        facts = plan_phase3_actions(drawers)

        # Verify each fact can be JSON-serialized as an MCP payload (no errors)
        for fact in facts:
            payload = json.dumps(
                {
                    "tool": "mempalace_kg_add",
                    "arguments": {
                        "subject": fact.subject,
                        "predicate": fact.predicate,
                        "object": fact.object,
                    },
                }
            )
            assert payload  # just ensure it serializes
