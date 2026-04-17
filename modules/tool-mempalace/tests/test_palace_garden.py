"""
Tests for PalaceTool `garden` operation and garden.py helpers.

Section 8.3 of spec-v1.2.0-gene-transfer.md
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from amplifier_module_tool_mempalace import PalaceTool
from amplifier_module_tool_mempalace.garden import (
    cluster_id,
    find_clusters,
    classify_cluster,
    extract_common_terms,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _result_json(tool_result: Any) -> dict:
    return json.loads(tool_result.output)


# ---------------------------------------------------------------------------
# Section 8.3: Required tests
# ---------------------------------------------------------------------------


class TestGardenOperationRegistered:
    def test_garden_operation_registered(self) -> None:
        """'garden' must appear in the operation enum."""
        tool = PalaceTool()
        enum_values: list[str] = tool.parameters["properties"]["operation"]["enum"]
        assert "garden" in enum_values, f"'garden' not in {enum_values}"


class TestFindClustersBasic:
    def test_find_clusters_basic(self) -> None:
        """Known adjacency → correct connected components."""
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
        """Two separate groups both ≥3 → 2 clusters returned."""
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
            "B": ["A"],  # component size 2 → excluded
            "C": [],  # singleton → excluded
            "D": ["E", "F"],
            "E": ["D"],
            "F": ["D"],  # component size 3 → included
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
        """Empty adjacency → empty result."""
        assert find_clusters({}, min_size=3) == []

    def test_find_clusters_all_singletons(self) -> None:
        """Nodes with no edges → no clusters."""
        adjacency = {"A": [], "B": [], "C": []}
        assert find_clusters(adjacency, min_size=3) == []


class TestClusterIdStable:
    def test_cluster_id_stable(self) -> None:
        """Same member set → same cluster hash (idempotency)."""
        members_a = {"drawer_1", "drawer_2", "drawer_3"}
        members_b = {"drawer_3", "drawer_1", "drawer_2"}  # different insertion order
        assert cluster_id(members_a) == cluster_id(members_b)

    def test_cluster_id_different_sets(self) -> None:
        """Different member sets → different cluster IDs."""
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
        """max_drawers=10 → at most 10 check_duplicate MCP calls (mocked)."""
        call_log: list[str] = []

        def fake_mcp_call(tool_name: str, args: dict) -> dict:
            call_log.append(tool_name)
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {"name": "wing_test", "rooms": [{"name": "r1", "count": 100}]}
                    ]
                }
            if tool_name == "mempalace_search":
                # Return 20 drawers regardless of limit requested
                return {
                    "results": [
                        {
                            "id": f"d{i:03d}",
                            "text": f"content {i}",
                            "room": "r1",
                            "metadata": {},
                        }
                        for i in range(20)
                    ]
                }
            if tool_name == "mempalace_check_duplicate":
                return {"matches": []}
            if tool_name == "mempalace_kg_query":
                return {"facts": []}
            if tool_name == "mempalace_kg_add":
                return {"ok": True}
            if tool_name == "mempalace_diary_write":
                return {"ok": True}
            return {}

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call",
            side_effect=fake_mcp_call,
        ):
            tool = PalaceTool()
            _run(
                tool.execute(
                    operation="garden",
                    wing="wing_test",
                    max_drawers=10,
                    cluster_threshold=0.80,
                )
            )

        # Count check_duplicate calls — must be ≤ 10
        dup_calls = call_log.count("mempalace_check_duplicate")
        assert dup_calls <= 10, (
            f"Expected ≤10 check_duplicate calls with max_drawers=10, got {dup_calls}"
        )

    def test_hard_cap_500(self) -> None:
        """max_drawers > 500 is silently clamped to 500."""
        call_log: list[int] = []

        def fake_mcp_call(tool_name: str, args: dict) -> dict:
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {"name": "wing_test", "rooms": [{"name": "r1", "count": 1000}]}
                    ]
                }
            if tool_name == "mempalace_search":
                # Return 100 drawers per search
                return {
                    "results": [
                        {
                            "id": f"d{i:04d}",
                            "text": f"content {i}",
                            "room": "r1",
                            "metadata": {},
                        }
                        for i in range(100)
                    ]
                }
            if tool_name == "mempalace_check_duplicate":
                call_log.append(1)
                return {"matches": []}
            if tool_name in (
                "mempalace_kg_query",
                "mempalace_kg_add",
                "mempalace_diary_write",
            ):
                return {"facts": [], "ok": True}
            return {}

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call",
            side_effect=fake_mcp_call,
        ):
            tool = PalaceTool()
            _run(
                tool.execute(
                    operation="garden",
                    wing="wing_test",
                    max_drawers=9999,  # should be clamped to 500
                )
            )

        assert len(call_log) <= 500, f"Hard cap violated: {len(call_log)} calls"


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
        """Tie in category votes → alphabetically first wins."""
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
        """No category tags → label uses 'uncategorized'."""
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
        """cluster with members in 2 rooms → expected spans_rooms edge emitted."""
        spans_rooms_calls: list[dict] = []

        def fake_mcp_call(tool_name: str, args: dict) -> dict:
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {
                            "name": "wing_x",
                            "rooms": [
                                {"name": "room_a", "count": 5},
                                {"name": "room_b", "count": 5},
                            ],
                        }
                    ]
                }
            if tool_name == "mempalace_search":
                room = args.get("room", "room_a")
                if room == "room_a":
                    return {
                        "results": [
                            {
                                "id": "d001",
                                "text": "text about auth",
                                "room": "room_a",
                                "metadata": {},
                            },
                            {
                                "id": "d002",
                                "text": "text about auth migration",
                                "room": "room_a",
                                "metadata": {},
                            },
                        ]
                    }
                return {
                    "results": [
                        {
                            "id": "d003",
                            "text": "text about auth oauth",
                            "room": "room_b",
                            "metadata": {},
                        },
                    ]
                }
            if tool_name == "mempalace_check_duplicate":
                # d001, d002, d003 all similar → full cluster
                content = args.get("content", "")
                if "auth" in content:
                    return {"matches": [{"id": "d001"}, {"id": "d002"}, {"id": "d003"}]}
                return {"matches": []}
            if tool_name == "mempalace_kg_add":
                if args.get("predicate") == "spans_rooms":
                    spans_rooms_calls.append(args)
                return {"ok": True}
            if tool_name == "mempalace_kg_query":
                return {"facts": []}
            if tool_name == "mempalace_diary_write":
                return {"ok": True}
            return {}

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call",
            side_effect=fake_mcp_call,
        ):
            tool = PalaceTool()
            _run(
                tool.execute(
                    operation="garden",
                    wing="wing_x",
                    max_drawers=50,
                )
            )

        # Cluster spans room_a and room_b → should have a spans_rooms edge
        assert len(spans_rooms_calls) >= 1, (
            "Expected spans_rooms KG edge for cross-room cluster"
        )


# ---------------------------------------------------------------------------
# Integration test (mempalace CLI)
# ---------------------------------------------------------------------------


import subprocess  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402


# ---------------------------------------------------------------------------
# Fix 1: Lookback filter
# ---------------------------------------------------------------------------


class TestLookbackFilter:
    """Verify that _get_drawers_in_scope applies the lookback_days filter."""

    def _make_fake_mcp(self, drawers_with_ts: list[tuple[str, str | None]]) -> Any:
        """Return a fake _mcp_call function that serves the given drawers.

        Each item is (drawer_id, iso_timestamp_or_None).
        """

        def fake_mcp(tool_name: str, args: dict) -> dict:
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {
                            "name": "wing_test",
                            "rooms": [{"name": "room_a", "count": 20}],
                        }
                    ]
                }
            if tool_name == "mempalace_search":
                results = []
                for did, ts in drawers_with_ts:
                    meta: dict = {}
                    if ts is not None:
                        meta["created_at"] = ts
                    results.append(
                        {
                            "id": did,
                            "text": f"content {did}",
                            "room": "room_a",
                            "metadata": meta,
                        }
                    )
                return {"results": results}
            return {}

        return fake_mcp

    def test_lookback_days_filters_old_drawers(self) -> None:
        """Drawers older than lookback_days are excluded; no-timestamp drawers are kept."""
        now = datetime.now(UTC)
        recent_ts = (now - timedelta(days=10)).isoformat()  # 10 days old → keep
        old_ts = (now - timedelta(days=60)).isoformat()  # 60 days old → drop
        no_ts = None  # no timestamp → keep (best-effort)

        drawers_with_ts = [
            ("d_recent", recent_ts),
            ("d_old", old_ts),
            ("d_no_ts", no_ts),
        ]

        from amplifier_module_tool_mempalace.garden import _get_drawers_in_scope

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call",
            side_effect=self._make_fake_mcp(drawers_with_ts),
        ):
            result = _get_drawers_in_scope(
                wing="wing_test",
                room=None,
                max_drawers=50,
                lookback_days=30,  # cutoff = 30 days ago
            )

        result_ids = {d["id"] for d in result}
        assert "d_recent" in result_ids, "Recent drawer should be included"
        assert "d_no_ts" in result_ids, (
            "No-timestamp drawer should be included (best-effort)"
        )
        assert "d_old" not in result_ids, "Old drawer should be excluded"

    def test_lookback_days_default_90(self) -> None:
        """89-day-old drawer kept; 91-day-old dropped; default lookback_days=90."""
        now = datetime.now(UTC)
        ts_89 = (now - timedelta(days=89)).isoformat()
        ts_91 = (now - timedelta(days=91)).isoformat()

        drawers_with_ts = [
            ("d_89", ts_89),
            ("d_91", ts_91),
        ]

        from amplifier_module_tool_mempalace.garden import _get_drawers_in_scope

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call",
            side_effect=self._make_fake_mcp(drawers_with_ts),
        ):
            result = _get_drawers_in_scope(
                wing="wing_test",
                room=None,
                max_drawers=50,
                lookback_days=90,  # default
            )

        result_ids = {d["id"] for d in result}
        assert "d_89" in result_ids
        assert "d_91" not in result_ids

    def test_lookback_epoch_timestamp(self) -> None:
        """Numeric epoch timestamps are parsed correctly."""
        now = datetime.now(UTC)
        recent_epoch = (now - timedelta(days=5)).timestamp()  # float, 5 days ago → keep
        old_epoch = (
            now - timedelta(days=100)
        ).timestamp()  # float, 100 days ago → drop

        def fake_mcp(tool_name: str, args: dict) -> dict:
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {"name": "wing_test", "rooms": [{"name": "r", "count": 5}]}
                    ]
                }
            if tool_name == "mempalace_search":
                return {
                    "results": [
                        {
                            "id": "d_recent",
                            "text": "t",
                            "room": "r",
                            "metadata": {"created_at": recent_epoch},
                        },
                        {
                            "id": "d_old",
                            "text": "t",
                            "room": "r",
                            "metadata": {"created_at": old_epoch},
                        },
                    ]
                }
            return {}

        from amplifier_module_tool_mempalace.garden import _get_drawers_in_scope

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call", side_effect=fake_mcp
        ):
            result = _get_drawers_in_scope("wing_test", None, 50, lookback_days=30)

        ids = {d["id"] for d in result}
        assert "d_recent" in ids
        assert "d_old" not in ids


# ---------------------------------------------------------------------------
# Fix 2: Total operation timeout
# ---------------------------------------------------------------------------


class TestGardenTotalTimeout:
    """Verify that execute_garden is bounded by the 120s wall-clock budget."""

    @pytest.mark.asyncio
    async def test_garden_total_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Slow MCP calls → garden returns a timeout error within the budget."""
        import time

        import amplifier_module_tool_mempalace as tm_init

        # Patch the timeout constant to 0.5s so we don't wait 120s in tests
        monkeypatch.setattr(tm_init, "_GARDEN_TIMEOUT_S", 0.5)

        call_count = 0

        def slow_mcp(tool_name: str, args: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [
                        {"name": "wing_test", "rooms": [{"name": "r1", "count": 50}]}
                    ]
                }
            if tool_name == "mempalace_search":
                return {
                    "results": [
                        {
                            "id": f"d{i:03d}",
                            "text": f"content {i}",
                            "room": "r1",
                            "metadata": {},
                        }
                        for i in range(20)
                    ]
                }
            # Each check_duplicate sleeps 0.2s to simulate slow palace
            if tool_name == "mempalace_check_duplicate":
                time.sleep(0.2)
                return {"matches": []}
            return {}

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call", side_effect=slow_mcp
        ):
            tool = PalaceTool()
            result = await tool.execute(
                operation="garden",
                wing="wing_test",
                max_drawers=50,
            )

        # Should return a timeout error (not hang until all 50 × 0.2s = 10s complete)
        assert not result.success, (
            f"Expected timeout result, got: success={result.success} output={result.output}"
        )

    @pytest.mark.asyncio
    async def test_timeout_emits_garden_completed_with_timed_out_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On timeout, garden_completed(ok=False, timed_out=True) is emitted."""
        import time

        import amplifier_module_tool_mempalace as tm_init

        monkeypatch.setattr(tm_init, "_GARDEN_TIMEOUT_S", 0.5)

        # Capture emitted events by patching the symbol in the __init__ module
        # (patching event_emitter.emit_event would miss the already-bound import)
        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            tm_init, "emit_event", lambda *a, **kw: emitted.append((a, kw))
        )

        def slow_mcp(tool_name: str, args: dict) -> dict:
            if tool_name == "mempalace_get_taxonomy":
                return {
                    "wings": [{"name": "wing_t", "rooms": [{"name": "r", "count": 20}]}]
                }
            if tool_name == "mempalace_search":
                return {
                    "results": [
                        {"id": f"d{i}", "text": f"t{i}", "room": "r", "metadata": {}}
                        for i in range(10)
                    ]
                }
            if tool_name == "mempalace_check_duplicate":
                time.sleep(0.2)
                return {"matches": []}
            return {}

        with patch(
            "amplifier_module_tool_mempalace.garden._mcp_call", side_effect=slow_mcp
        ):
            tool = PalaceTool()
            result = await tool.execute(
                operation="garden", wing="wing_t", max_drawers=20
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
        assert len(timeout_events) == 1, (
            f"Expected exactly one garden_completed(ok=False, timed_out=True) event.\n"
            f"Emitted events: {[(a[1] if len(a) > 1 else '?', kw.get('ok'), kw.get('data', {}).get('timed_out')) for a, kw in emitted]}"
        )


def _mempalace_available() -> bool:
    try:
        result = subprocess.run(
            ["mempalace", "--version"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _mempalace_available(), reason="mempalace CLI not available")
class TestGardenIntegration:
    def test_garden_operation_accepted(self) -> None:
        """Garden operation completes without error (real palace)."""
        tool = PalaceTool()
        result = _run(tool.execute(operation="garden", wing="wing_test", max_drawers=5))
        payload = _result_json(result)
        assert "drawers_analyzed" in payload
        assert "clusters" in payload
