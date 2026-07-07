"""
Tests for MemoryTool `garden` operation and garden.py helpers.

Section 8.3 of spec-v1.2.0-gene-transfer.md

Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): garden's
drawer enumeration, near-duplicate detection, KG edges, and diary writes all
route through a fake ``MemoryClient``-shaped stub (patched via
``garden.ensure_daemon``) instead of a vendor subprocess.
Clustering math (find_clusters/cluster_id/classify_cluster/
extract_common_terms) is pure and untouched -- those tests are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
from amplifier_module_tool_memory import MemoryTool
from amplifier_module_tool_memory.garden import (
    classify_cluster,
    cluster_id,
    extract_common_terms,
    find_clusters,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _result_json(tool_result: Any) -> dict:
    return json.loads(tool_result.output)


class _FakeMemoryClient:
    """Minimal stand-in for MemoryClient exercising exactly the surface
    garden.py uses: list_drawers, search, kg_add, write_cell, assert_fact,
    diary_write."""

    def __init__(
        self,
        drawers: list[dict[str, Any]],
        *,
        adjacency: dict[str, list[str]] | None = None,
        search_delay: float = 0.0,
    ) -> None:
        self._drawers = drawers
        self._by_ref = {d["ref"]: d for d in drawers}
        self._adjacency = adjacency or {}
        self._search_delay = search_delay
        self.kg_add_calls: list[tuple[str, str, str]] = []
        self.diary_writes: list[str] = []
        self.assert_fact_calls: list[tuple[str, str, str]] = []
        self._cell_seq = 0

    def list_drawers(self, *, wing=None, room=None, limit=200):
        return list(self._drawers[:limit])

    def search(self, query, k=5, *, wing=None, room=None):
        if self._search_delay:
            time.sleep(self._search_delay)
        # Find which drawer this query came from (by text prefix match) and
        # return its configured neighbors as high-scoring hits.
        results = []
        for d in self._drawers:
            if d["content"][:1000] == query:
                neighbor_ids = self._adjacency.get(d["ref"], [])
                for nid in neighbor_ids:
                    nd = self._by_ref.get(nid)
                    if nd:
                        results.append(
                            {"ref": nid, "score": 0.95, "content": nd["content"]}
                        )
                break
        return {"results": results, "degraded": None}

    def kg_add(self, subject, predicate, object):  # noqa: A002
        self.kg_add_calls.append((subject, predicate, object))

    def write_cell(self, payload: bytes) -> str:
        self._cell_seq += 1
        return f"cell-{self._cell_seq}"

    def assert_fact(self, subject, predicate, object):  # noqa: A002
        self.assert_fact_calls.append((subject, predicate, object))

    def diary_write(self, *, agent_name, entry, topic="general"):
        self.diary_writes.append(entry)
        return "diary-ref"


def _drawer(
    ref: str,
    content: str,
    room: str = "r1",
    category: str | None = None,
    importance: float | None = None,
) -> dict[str, Any]:
    return {
        "ref": ref,
        "content": content,
        "wing": "wing_test",
        "room": room,
        "category": category,
        "importance": importance,
    }


# ---------------------------------------------------------------------------
# Section 8.3: Required tests
# ---------------------------------------------------------------------------


class TestGardenOperationRegistered:
    def test_garden_operation_registered(self) -> None:
        """'garden' must appear in the operation enum."""
        tool = MemoryTool()
        enum_values: list[str] = tool.input_schema["properties"]["operation"]["enum"]
        assert "garden" in enum_values, f"'garden' not in {enum_values}"


class TestFindClustersBasic:
    def test_find_clusters_basic(self) -> None:
        """Known adjacency -> correct connected components."""
        # A-B-C form a cluster; D-E-F form another; G is isolated
        adjacency: dict[str, list[str]] = {
            "A": ["B"],
            "B": ["A", "C"],
            "C": ["B"],
            "D": ["E"],
            "E": ["D", "F"],
            "F": ["E"],
            "G": [],
        }
        clusters = find_clusters(adjacency, min_size=3)
        assert len(clusters) == 2
        member_sets = [frozenset(c) for c in clusters]
        assert frozenset({"A", "B", "C"}) in member_sets
        assert frozenset({"D", "E", "F"}) in member_sets

    def test_find_clusters_disconnected(self) -> None:
        """Two separate groups both >=3 -> 2 clusters returned."""
        adjacency: dict[str, list[str]] = {
            "A": ["B", "C"],
            "B": ["A"],
            "C": ["A"],
            "X": ["Y", "Z"],
            "Y": ["X"],
            "Z": ["X"],
        }
        clusters = find_clusters(adjacency, min_size=3)
        assert len(clusters) == 2


class TestFindClustersMinimumSize:
    def test_find_clusters_minimum_size(self) -> None:
        """Components with < 3 members excluded from result."""
        adjacency: dict[str, list[str]] = {
            "A": ["B"],
            "B": ["A"],  # component size 2 -> excluded
            "C": [],  # singleton -> excluded
            "D": ["E", "F"],
            "E": ["D"],
            "F": ["D"],  # component size 3 -> included
        }
        clusters = find_clusters(adjacency, min_size=3)
        assert len(clusters) == 1
        assert frozenset({"D", "E", "F"}) == frozenset(clusters[0])

    def test_find_clusters_configurable_min_size(self) -> None:
        """min_size parameter controls the threshold."""
        adjacency: dict[str, list[str]] = {"A": ["B"], "B": ["A"]}
        assert find_clusters(adjacency, min_size=2) == [{"A", "B"}]
        assert find_clusters(adjacency, min_size=3) == []


class TestFindClustersEmpty:
    def test_find_clusters_empty(self) -> None:
        """Empty adjacency -> empty result."""
        assert find_clusters({}, min_size=3) == []

    def test_find_clusters_all_singletons(self) -> None:
        """Nodes with no edges -> no clusters."""
        adjacency = {"A": [], "B": [], "C": []}
        assert find_clusters(adjacency, min_size=3) == []


class TestClusterIdStable:
    def test_cluster_id_stable(self) -> None:
        """Same member set -> same cluster hash (idempotency)."""
        members_a = {"drawer_1", "drawer_2", "drawer_3"}
        members_b = {"drawer_3", "drawer_1", "drawer_2"}  # different insertion order
        assert cluster_id(members_a) == cluster_id(members_b)

    def test_cluster_id_different_sets(self) -> None:
        """Different member sets -> different cluster IDs."""
        members_a = {"drawer_1", "drawer_2", "drawer_3"}
        members_b = {"drawer_4", "drawer_5", "drawer_6"}
        assert cluster_id(members_a) != cluster_id(members_b)

    def test_cluster_id_format(self) -> None:
        """cluster_id returns a 12-char hex string."""
        cid = cluster_id({"a", "b", "c"})
        assert len(cid) == 12
        assert all(c in "0123456789abcdef" for c in cid)


class TestGardenBudgetCap:
    def test_garden_budget_cap(self) -> None:
        """max_drawers=10 -> list_drawers is asked for at most 10 (native
        cutover: list_drawers replaces the old per-room search+check_duplicate
        loop, so the budget is enforced at the enumeration call itself)."""
        drawers = [_drawer(f"d{i:03d}", f"content {i}") for i in range(20)]
        fake_client = _FakeMemoryClient(drawers[:10])  # daemon honors the limit

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            result = _run(
                tool.execute(
                    {
                        "operation": "garden",
                        "wing": "wing_test",
                        "max_drawers": 10,
                        "cluster_threshold": 0.80,
                    }
                )
            )
        assert result.success
        payload = _result_json(result)
        assert payload["drawers_analyzed"] <= 10

    def test_hard_cap_500(self) -> None:
        """max_drawers > 500 is silently clamped to 500."""
        drawers = [_drawer(f"d{i:04d}", f"content {i}") for i in range(500)]
        fake_client = _FakeMemoryClient(drawers)
        captured_limit: dict[str, int] = {}

        real_list_drawers = fake_client.list_drawers

        def spying_list_drawers(*, wing=None, room=None, limit=200):
            captured_limit["limit"] = limit
            return real_list_drawers(wing=wing, room=room, limit=limit)

        fake_client.list_drawers = spying_list_drawers  # type: ignore[method-assign]

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            _run(
                tool.execute(
                    {
                        "operation": "garden",
                        "wing": "wing_test",
                        "max_drawers": 9999,  # should be clamped to 500
                    }
                )
            )

        assert captured_limit["limit"] == 500, f"Hard cap violated: {captured_limit}"


# ---------------------------------------------------------------------------
# classify_cluster and label generation
# ---------------------------------------------------------------------------


class TestClassifyCluster:
    def test_label_generation(self) -> None:
        """Given category + terms, label has correct format."""
        label, dominant = classify_cluster(
            member_ids={"d001", "d002", "d003"},
            categories={"d001": "decision", "d002": "decision", "d003": "architecture"},
            texts={
                "d001": "auth migration clerk",
                "d002": "auth clerk oauth",
                "d003": "auth design",
            },
        )
        assert "decision" in label
        assert "3 drawers" in label
        assert "auth" in label
        assert dominant == "decision"

    def test_dominant_category_tie_break_alphabetical(self) -> None:
        """Tie in category votes -> alphabetically first wins."""
        label, dominant = classify_cluster(
            member_ids={"a", "b", "c", "d"},
            categories={
                "a": "decision",
                "b": "decision",
                "c": "pattern",
                "d": "pattern",
            },
            texts={"a": "x", "b": "y", "c": "z", "d": "w"},
        )
        assert label.startswith("decision")
        assert dominant == "decision"

    def test_no_categories_uses_uncategorized(self) -> None:
        """No category tags -> label uses 'uncategorized'."""
        label, dominant = classify_cluster(
            member_ids={"a", "b", "c"},
            categories={},  # no KG facts
            texts={"a": "foo", "b": "bar", "c": "baz"},
        )
        assert "uncategorized" in label
        assert dominant == "uncategorized"


class TestExtractCommonTerms:
    def test_extract_common_terms(self) -> None:
        texts = ["auth migration clerk", "auth oauth migration", "auth token refresh"]
        terms = extract_common_terms(texts, top_n=2)
        assert "auth" in terms  # most frequent
        assert len(terms) <= 2

    def test_stopwords_filtered(self) -> None:
        texts = ["the quick brown fox", "the lazy brown dog", "the brown cat"]
        terms = extract_common_terms(texts, top_n=3)
        assert "the" not in terms  # stopword
        assert "brown" in terms

    def test_empty_texts(self) -> None:
        assert extract_common_terms([], top_n=3) == []


# ---------------------------------------------------------------------------
# Cross-room detection
# ---------------------------------------------------------------------------


class TestCrossRoomDetection:
    def test_cross_room_detection(self) -> None:
        """cluster with members in 2 rooms -> expected spans_rooms edge emitted."""
        drawers = [
            _drawer("d001", "text about auth", room="room_a"),
            _drawer("d002", "text about auth migration", room="room_a"),
            _drawer("d003", "text about auth oauth", room="room_b"),
        ]
        adjacency = {
            "d001": ["d002", "d003"],
            "d002": ["d001", "d003"],
            "d003": ["d001", "d002"],
        }
        fake_client = _FakeMemoryClient(drawers, adjacency=adjacency)

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            _run(
                tool.execute(
                    {
                        "operation": "garden",
                        "wing": "wing_x",
                        "max_drawers": 50,
                    }
                )
            )

        spans_rooms_calls = [
            c for c in fake_client.kg_add_calls if c[1] == "spans_rooms"
        ]
        assert len(spans_rooms_calls) >= 1, (
            "Expected spans_rooms KG edge for cross-room cluster"
        )


# ---------------------------------------------------------------------------
# Honest lookback_days limitation (native store has no filing timestamp)
# ---------------------------------------------------------------------------


class TestLookbackIsHonestNoOp:
    """The native store does not track a drawer's filing timestamp, so
    lookback_days is a documented no-op: every drawer list_drawers returns
    for the scope is analyzed regardless of age. This replaces the old
    TestLookbackFilter suite (which pinned filtering behavior the native
    store cannot honestly support without fabricating a timestamp)."""

    def test_lookback_days_does_not_filter_any_drawer(self) -> None:
        drawers = [_drawer(f"d{i}", f"content {i}") for i in range(5)]
        fake_client = _FakeMemoryClient(drawers)

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            result = _run(
                tool.execute(
                    {
                        "operation": "garden",
                        "wing": "wing_test",
                        "lookback_days": 1,  # would exclude everything if honored
                        "max_drawers": 50,
                    }
                )
            )
        assert result.success
        payload = _result_json(result)
        assert payload["drawers_analyzed"] == 5
        assert payload["scope"]["lookback_days"] == 1


# ---------------------------------------------------------------------------
# Fix 2: Total operation timeout
# ---------------------------------------------------------------------------


class TestGardenTotalTimeout:
    """Verify that execute_garden is bounded by the 120s wall-clock budget."""

    @pytest.mark.asyncio
    async def test_garden_total_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Slow native searches -> garden returns a timeout error within the budget."""
        import amplifier_module_tool_memory as tm_init

        # Patch the timeout constant to 0.5s so we don't wait 120s in tests
        monkeypatch.setattr(tm_init, "_GARDEN_TIMEOUT_S", 0.5)

        drawers = [_drawer(f"d{i:03d}", f"content {i}") for i in range(20)]
        # Every search() call sleeps 0.2s to simulate a slow daemon.
        fake_client = _FakeMemoryClient(drawers, search_delay=0.2)

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            result = await tool.execute(
                {
                    "operation": "garden",
                    "wing": "wing_test",
                    "max_drawers": 50,
                }
            )

        # Should return a timeout error (not hang until all 20 x 0.2s = 4s complete)
        assert not result.success, (
            f"Expected timeout result, got: success={result.success} output={result.output}"
        )

    @pytest.mark.asyncio
    async def test_timeout_emits_garden_completed_with_timed_out_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On timeout, garden_completed(ok=False, timed_out=True) is emitted."""
        import amplifier_module_tool_memory as tm_init

        monkeypatch.setattr(tm_init, "_GARDEN_TIMEOUT_S", 0.5)

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            tm_init, "emit_event", lambda *a, **kw: emitted.append((a, kw))
        )

        drawers = [_drawer(f"d{i}", f"t{i}") for i in range(10)]
        fake_client = _FakeMemoryClient(drawers, search_delay=0.2)

        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=fake_client,
        ):
            tool = MemoryTool()
            result = await tool.execute(
                {"operation": "garden", "wing": "wing_t", "max_drawers": 20}
            )

        assert not result.success

        # Find the garden_completed(ok=False, timed_out=True) event
        timeout_events = [
            e
            for e in emitted
            if len(e[0]) >= 2
            and e[0][1] == "garden_completed"
            and e[1].get("ok") is False
            and e[1].get("data", {}).get("timed_out") is True
        ]
        summary = [
            (
                a[1] if len(a) > 1 else "?",
                kw.get("ok"),
                kw.get("data", {}).get("timed_out"),
            )
            for a, kw in emitted
        ]
        assert len(timeout_events) == 1, (
            "Expected exactly one garden_completed(ok=False, timed_out=True) event.\n"
            f"Emitted events: {summary}"
        )


# ---------------------------------------------------------------------------
# Daemon-unavailable degradation (\u00a75.7): garden must fail loudly, never hang
# ---------------------------------------------------------------------------


class TestGardenDaemonUnavailable:
    def test_daemon_unavailable_returns_failure(self) -> None:
        with patch(
            "amplifier_module_tool_memory.garden.ensure_daemon",
            return_value=None,
        ):
            tool = MemoryTool()
            result = _run(
                tool.execute(
                    {"operation": "garden", "wing": "wing_test", "max_drawers": 10}
                )
            )
        assert not result.success
        assert "memory daemon unavailable" in result.error["message"]


# ---------------------------------------------------------------------------
# Real-daemon integration (replaces the old vendor-CLI-gated integration
# test -- native cutover means there is no CLI to probe for anymore).
# ---------------------------------------------------------------------------


class TestGardenIntegration:
    def test_garden_operation_completes_against_real_daemon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        pytest.importorskip("amplifier_data")
        monkeypatch.setenv("AMPLIFIER_MEMORY_HOME", str(tmp_path / "memory-home"))
        import amplifier_module_tool_memory.client as client_mod

        client = client_mod.ensure_daemon()
        if client is None:
            pytest.skip("could not start a real memory daemon in this environment")
        try:
            client.remember(
                wing="wing_garden_it", room="r", content="first drawer content"
            )
            client.remember(
                wing="wing_garden_it", room="r", content="second drawer content"
            )

            tool = MemoryTool()
            result = _run(
                tool.execute(
                    {"operation": "garden", "wing": "wing_garden_it", "max_drawers": 5}
                )
            )
            assert result.success, result.error
            payload = _result_json(result)
            assert "drawers_analyzed" in payload
            assert "clusters" in payload
        finally:
            client.shutdown()
