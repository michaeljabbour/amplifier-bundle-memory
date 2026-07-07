"""
Tests for the manifest-driven importance override seam in phase3.

The capture manifest declares ``importance_base`` per category. These tests
pin the ``base_overrides`` parameter that lets the Curator feed those declared
bases into the existing rubric WITHOUT changing default (no-override) behavior.
"""

from __future__ import annotations

import pytest

from amplifier_module_tool_memory.phase3 import (
    DrawerRecord,
    compute_importance,
    plan_phase3_actions,
)


class TestComputeImportanceOverrides:
    def test_override_replaces_base(self) -> None:
        assert (
            compute_importance("decision", {}, base_overrides={"decision": 0.90})
            == 0.90
        )

    def test_override_then_boost(self) -> None:
        # overridden base 0.50 + user_explicit boost 0.15 = 0.65
        score = compute_importance(
            "decision", {"user_explicit": True}, base_overrides={"decision": 0.50}
        )
        assert score == pytest.approx(0.65)

    def test_override_respects_rubric_cap(self) -> None:
        # lesson_learned cap is 0.7; 0.69 + 0.10 = 0.79 -> capped to 0.7
        score = compute_importance(
            "lesson_learned",
            {"gotcha_adjacent": True},
            base_overrides={"lesson_learned": 0.69},
        )
        assert score == pytest.approx(0.70)

    def test_override_for_unknown_category_raises_cap(self) -> None:
        # A manifest-only category (no rubric entry) must honor its declared
        # base rather than being clamped to the uncategorized cap of 0.5.
        score = compute_importance(
            "api_contract", {}, base_overrides={"api_contract": 0.80}
        )
        assert score == pytest.approx(0.80)

    def test_override_only_affects_named_category(self) -> None:
        # override names 'decision'; blocker keeps its rubric base 0.65
        assert (
            compute_importance("blocker", {}, base_overrides={"decision": 0.99}) == 0.65
        )

    def test_no_override_matches_legacy(self) -> None:
        assert compute_importance("decision", {}) == 0.75
        assert compute_importance("decision", {}, base_overrides=None) == 0.75


class TestPlanPhase3WithOverrides:
    def test_plan_applies_override(self) -> None:
        drawers = [
            DrawerRecord(id="d1", category="pattern", signals={}, dup_match=None)
        ]
        facts = plan_phase3_actions(drawers, base_overrides={"pattern": 0.66})
        imp = [f for f in facts if f.predicate == "has_importance"]
        assert float(imp[0].object) == pytest.approx(0.66)

    def test_plan_without_override_matches_legacy(self) -> None:
        drawers = [
            DrawerRecord(id="d1", category="pattern", signals={}, dup_match=None)
        ]
        facts = plan_phase3_actions(drawers)
        imp = [f for f in facts if f.predicate == "has_importance"]
        assert float(imp[0].object) == pytest.approx(0.50)

    def test_override_does_not_apply_to_duplicate_override(self) -> None:
        # near-identical duplicate still forces importance 0.15 regardless of base
        drawers = [
            DrawerRecord(
                id="d1", category="decision", signals={}, dup_match=("d0", 0.97)
            )
        ]
        facts = plan_phase3_actions(drawers, base_overrides={"decision": 0.99})
        imp = [f for f in facts if f.predicate == "has_importance"]
        assert float(imp[0].object) == pytest.approx(0.15)
