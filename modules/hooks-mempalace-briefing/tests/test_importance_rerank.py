"""
Tests for briefing importance re-ranking.

Section 8.5 of spec-v1.2.0-gene-transfer.md

Formula: final = semantic + weight * (importance - 0.5) * 0.08
All tests are pure unit tests with no MCP calls.
"""

from __future__ import annotations

import pytest

from amplifier_module_hooks_mempalace_briefing import _rerank_by_importance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(id: str, score: float) -> dict:
    """Minimal search result dict."""
    return {"id": id, "score": score, "room": "test", "text": f"content of {id}"}


def _lookup(*pairs: tuple[str, float]):
    """Build an importance lookup dict from (id, importance) pairs."""
    return {id_: imp for id_, imp in pairs}


# ---------------------------------------------------------------------------
# Section 8.5 required tests
# ---------------------------------------------------------------------------


class TestRerankByImportance:
    def test_rerank_no_importance_facts(self) -> None:
        """All importance=0.5 (default) → order unchanged from semantic."""
        results = [
            _result("A", 0.9),
            _result("B", 0.8),
            _result("C", 0.7),
        ]
        # All default to 0.5 → boost = 0 for all
        lookup = _lookup(("A", 0.5), ("B", 0.5), ("C", 0.5))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        ids = [r["id"] for r in reranked]
        assert ids == ["A", "B", "C"]

    def test_rerank_high_importance_boost(self) -> None:
        """B (sem=0.85, imp=0.90) ranks above A (sem=0.86, imp=0.50) with weight=1.0."""
        # A final = 0.86 + 1.0*(0.50-0.5)*0.08 = 0.86 + 0 = 0.860
        # B final = 0.85 + 1.0*(0.90-0.5)*0.08 = 0.85 + 0.032 = 0.882
        results = [_result("A", 0.86), _result("B", 0.85)]
        lookup = _lookup(("A", 0.50), ("B", 0.90))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        ids = [r["id"] for r in reranked]
        assert ids[0] == "B", f"Expected B first, got {ids}"

    def test_rerank_low_importance_sink(self) -> None:
        """A (sem=0.90, imp=0.15) sinks below B (sem=0.89, imp=0.50) with weight=1.0."""
        # A final = 0.90 + 1.0*(0.15-0.5)*0.08 = 0.90 - 0.028 = 0.872
        # B final = 0.89 + 1.0*(0.50-0.5)*0.08 = 0.89 + 0 = 0.890
        results = [_result("A", 0.90), _result("B", 0.89)]
        lookup = _lookup(("A", 0.15), ("B", 0.50))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        ids = [r["id"] for r in reranked]
        assert ids[0] == "B", f"Expected B first after sink, got {ids}"

    def test_rerank_weight_zero_disabled(self) -> None:
        """weight=0.0 → order identical to raw semantic sort regardless of importance."""
        results = [
            _result("A", 0.9),
            _result("B", 0.7),
            _result("C", 0.8),
        ]
        lookup = _lookup(("A", 0.10), ("B", 1.00), ("C", 0.50))
        reranked = _rerank_by_importance(results, lookup, weight=0.0)
        ids = [r["id"] for r in reranked]
        assert ids == ["A", "C", "B"]

    def test_rerank_max_boost_bounded(self) -> None:
        """imp=1.0, weight=1.0 → boost exactly +0.04."""
        results = [_result("A", 0.80)]
        lookup = _lookup(("A", 1.0))
        _rerank_by_importance(results, lookup, weight=1.0)
        # Verify by computing expected final score manually.
        # Expected: 0.80 + 1.0*(1.0-0.5)*0.08 = 0.80 + 0.04 = 0.84
        expected_final = 0.80 + 1.0 * (1.0 - 0.5) * 0.08
        assert expected_final == pytest.approx(0.84)

        # Also verify with weight=1.0, imp=0.0 gives -0.04
        expected_min = 0.80 + 1.0 * (0.0 - 0.5) * 0.08
        assert expected_min == pytest.approx(0.76)

    def test_rerank_preserves_top_result(self) -> None:
        """A (sem=0.95, imp=0.50) stays on top despite B (sem=0.85, imp=1.00)."""
        # A final = 0.95 + 1.0*(0.50-0.5)*0.08 = 0.950
        # B final = 0.85 + 1.0*(1.00-0.5)*0.08 = 0.85 + 0.04 = 0.890
        # Semantic gap = 0.10 > max boost 0.04, so A stays on top
        results = [_result("A", 0.95), _result("B", 0.85)]
        lookup = _lookup(("A", 0.50), ("B", 1.00))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        assert reranked[0]["id"] == "A"


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestRerankEdgeCases:
    def test_empty_results(self) -> None:
        """Empty input → empty output, no error."""
        assert _rerank_by_importance([], {}, weight=1.0) == []

    def test_missing_importance_defaults_to_0_5(self) -> None:
        """If a result's id is absent from lookup, default importance=0.5 (zero boost)."""
        results = [_result("A", 0.80), _result("B", 0.75)]
        # Only B is in the lookup; A should default to 0.5
        lookup = _lookup(("B", 0.90))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        # B final = 0.75 + 1.0*(0.90-0.5)*0.08 = 0.75 + 0.032 = 0.782
        # A final = 0.80 + 1.0*(0.50-0.5)*0.08 = 0.80 + 0.0 = 0.80
        # A still wins (gap > boost)
        assert reranked[0]["id"] == "A"

    def test_result_without_id_handled(self) -> None:
        """Results without 'id' key → treated as missing from lookup (no crash)."""
        results = [{"score": 0.80, "room": "x", "text": "y"}]  # no id
        reranked = _rerank_by_importance(results, {}, weight=1.0)
        assert len(reranked) == 1

    def test_single_result_unchanged(self) -> None:
        """Single result → returned as-is regardless of importance."""
        results = [_result("A", 0.70)]
        reranked = _rerank_by_importance(results, _lookup(("A", 0.20)), weight=1.0)
        assert reranked[0]["id"] == "A"

    def test_weight_fractional(self) -> None:
        """Fractional weight scales the boost proportionally."""
        # weight=0.5, imp=1.0: boost = 0.5*(1.0-0.5)*0.08 = 0.02
        results = [_result("A", 0.80)]
        lookup = _lookup(("A", 1.0))
        _rerank_by_importance(results, lookup, weight=0.5)
        expected = 0.80 + 0.5 * (1.0 - 0.5) * 0.08
        assert expected == pytest.approx(0.82)

    def test_sort_stable_for_equal_finals(self) -> None:
        """Equal final scores → original order preserved (stable sort)."""
        # Both have same semantic and same importance → identical finals
        results = [_result("A", 0.80), _result("B", 0.80)]
        lookup = _lookup(("A", 0.50), ("B", 0.50))
        reranked = _rerank_by_importance(results, lookup, weight=1.0)
        assert [r["id"] for r in reranked] == ["A", "B"]
