"""Tests for the NEW native read surfaces on NativeMemoryStore
(\u00a73.2 of docs/plans/2026-07-07-native-cutover-design.md): search, list_drawers,
read_diary, status, kg_stats. Direct seam-level tests (no HTTP, no daemon) --
the daemon dispatch layer is covered separately in test_daemon.py.

Skipped entirely when amplifier-data is not installed (optional dependency,
same convention as test_amplifier_data_store.py).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (  # noqa: E402
    NativeMemoryStore,
)


def _file(store: NativeMemoryStore, **kw: Any) -> str:
    ref = store.file(**kw)
    return str(ref)


class TestListDrawers:
    def test_scoped_by_room(self) -> None:
        store = NativeMemoryStore(record_access=False)
        r1 = _file(store, wing="w1", room="auth", content="alpha")
        _file(store, wing="w1", room="other", content="beta")
        drawers = store.list_drawers(wing="w1", room="auth")
        refs = {d["ref"] for d in drawers}
        assert refs == {r1}
        assert drawers[0]["content"] == "alpha"
        assert drawers[0]["wing"] == "w1"
        assert drawers[0]["room"] == "auth"

    def test_scoped_by_wing_only(self) -> None:
        store = NativeMemoryStore(record_access=False)
        r1 = _file(store, wing="w2", room="a", content="one")
        r2 = _file(store, wing="w2", room="b", content="two")
        _file(store, wing="w3", room="a", content="three")
        drawers = store.list_drawers(wing="w2")
        refs = {d["ref"] for d in drawers}
        assert refs == {r1, r2}

    def test_category_and_importance_present(self) -> None:
        store = NativeMemoryStore(record_access=False)
        _file(
            store,
            wing="w4",
            room="r",
            content="tagged",
            category="decision",
            importance=0.9,
        )
        drawers = store.list_drawers(wing="w4", room="r")
        assert drawers[0]["category"] == "decision"
        assert drawers[0]["importance"] == 0.9

    def test_unknown_scope_returns_empty(self) -> None:
        store = NativeMemoryStore(record_access=False)
        assert store.list_drawers(wing="does-not-exist") == []

    def test_limit_is_respected(self) -> None:
        store = NativeMemoryStore(record_access=False)
        for i in range(5):
            _file(store, wing="w5", room="r", content=f"drawer {i}")
        assert len(store.list_drawers(wing="w5", room="r", limit=2)) == 2


class TestSearch:
    def test_vector_search_hybrid_rank(self) -> None:
        store = NativeMemoryStore(record_access=False)
        _file(store, wing="w6", room="r", content="we decided on the auth manifest")
        _file(store, wing="w6", room="r", content="unrelated content about cooking")
        # add embeddings directly (bypassing a real embedder -- this test pins
        # the seam's ranking math, not the embedder itself).
        s = store.store
        drawers = store.list_drawers(wing="w6", room="r")
        target = next(d for d in drawers if "auth" in d["content"])
        other = next(d for d in drawers if "cooking" in d["content"])
        s.add_embedding(target["ref"], [1.0, 0.0, 0.0])
        s.add_embedding(other["ref"], [0.0, 1.0, 0.0])

        results = store.search(
            [1.0, 0.0, 0.0], 2, wing="w6", room="r", lexical_query="auth manifest"
        )
        assert results[0]["ref"] == target["ref"]
        assert results[0]["score"] > results[1]["score"]

    def test_degraded_lexical_only_when_vector_is_none(self) -> None:
        store = NativeMemoryStore(record_access=False)
        _file(store, wing="w7", room="r", content="the manifest decision was final")
        _file(store, wing="w7", room="r", content="totally unrelated cooking content")
        results = store.search(
            None, 5, wing="w7", room="r", lexical_query="manifest decision"
        )
        assert results
        assert "manifest" in results[0]["content"]

    def test_empty_scope_returns_empty(self) -> None:
        store = NativeMemoryStore(record_access=False)
        assert store.search(None, 5, wing="no-such-wing", lexical_query="x") == []

    def test_needs_embedding_drawer_surfaced_via_lexical_union(self) -> None:
        """Cold-start data-loss fix: a drawer with a `needs_embedding` marker
        (filed while the embedder was not ready) and NO vector must still be
        found by a non-degraded (query_vector is not None) search, via the
        lexical-union hardening -- not just left unreachable until the sweep
        runs. Vector-scored hits still take priority in ranking."""
        store = NativeMemoryStore(record_access=False)
        s = store.store
        vectored_ref = _file(
            store, wing="w8", room="r", content="an embedded drawer about widgets"
        )
        s.add_embedding(vectored_ref, [1.0, 0.0, 0.0])

        pending_ref = _file(
            store, wing="w8", room="r", content="a pending widgets drawer no vector"
        )
        s.assert_fact(pending_ref, "needs_embedding", s.write_cell(b"true"))

        results = store.search(
            [1.0, 0.0, 0.0], 5, wing="w8", room="r", lexical_query="widgets"
        )
        refs = {r["ref"] for r in results}
        assert vectored_ref in refs
        assert pending_ref in refs
        # vector-scored candidate keeps priority in the ranking
        assert results[0]["ref"] == vectored_ref

    def test_no_needs_embedding_facts_skips_lexical_union(self) -> None:
        """Steady-state (no pending facts): search must not silently pick up
        an un-embedded, out-of-scope drawer via a full scan -- the hardening
        path is gated behind an actual `needs_embedding` fact existing."""
        store = NativeMemoryStore(record_access=False)
        s = store.store
        vectored_ref = _file(store, wing="w9", room="r", content="embedded only")
        s.add_embedding(vectored_ref, [1.0, 0.0, 0.0])
        # A second, un-embedded drawer with NO needs_embedding marker at all
        # (simulates content filed some other way, not via remember's
        # pre-ready path) must NOT appear -- there is nothing to converge.
        unmarked_ref = _file(store, wing="w9", room="r", content="embedded only too")

        results = store.search(
            [1.0, 0.0, 0.0], 5, wing="w9", room="r", lexical_query="embedded"
        )
        refs = {r["ref"] for r in results}
        assert refs == {vectored_ref}
        assert unmarked_ref not in refs


class TestReadDiary:
    def test_seq_pos_ordered_newest_last(self) -> None:
        store = NativeMemoryStore(record_access=False)
        store.file_diary(agent_name="curator", entry="first entry", topic="general")
        store.file_diary(agent_name="curator", entry="second entry", topic="general")
        store.file_diary(agent_name="curator", entry="third entry", topic="general")
        entries = store.read_diary(agent_name="curator", last_n=10)
        assert [e["entry"] for e in entries] == [
            "first entry",
            "second entry",
            "third entry",
        ]

    def test_last_n_caps_result(self) -> None:
        store = NativeMemoryStore(record_access=False)
        for i in range(5):
            store.file_diary(agent_name="curator2", entry=f"entry {i}")
        entries = store.read_diary(agent_name="curator2", last_n=2)
        assert [e["entry"] for e in entries] == ["entry 3", "entry 4"]

    def test_unknown_agent_returns_empty(self) -> None:
        store = NativeMemoryStore(record_access=False)
        assert store.read_diary(agent_name="nobody") == []


class TestKgStatsAndStatus:
    def test_kg_stats_counts_anchor_facts(self) -> None:
        store = NativeMemoryStore(record_access=False)
        store.assert_kg("alice", "knows", "bob")
        store.assert_kg("alice", "knows", "carol")
        stats = store.kg_stats()
        assert stats["facts"] >= 2
        assert stats["entities"] >= 3  # alice, bob, carol

    def test_kg_stats_ignores_non_entity_facts(self) -> None:
        store = NativeMemoryStore(record_access=False)
        ref = _file(store, wing="w8", room="r", content="x", category="decision")
        stats_before = store.kg_stats()
        # has_category on a plain drawer ref is NOT an entity: anchor fact.
        assert store.store.query_facts(subject=ref, predicate="has_category").success
        stats_after = store.kg_stats()
        assert stats_after["facts"] == stats_before["facts"]

    def test_status_reports_drawers_and_wings(self) -> None:
        store = NativeMemoryStore(record_access=False)
        _file(store, wing="status_w1", room="r", content="a")
        _file(store, wing="status_w2", room="r", content="b")
        st = store.status()
        assert st["drawers"] >= 2
        assert "status_w1" in st["wings"]
        assert "status_w2" in st["wings"]
        assert isinstance(st["kg_facts"], int)
