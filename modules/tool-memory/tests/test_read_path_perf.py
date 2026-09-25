"""perf/startup-latency: read paths that the session-start briefing (and the
per-prompt interject search) hit, made cheap without changing any result.

Pins, on a synthetic store (plus a durable one where the log matters):

* ``search`` hits carry ``importance`` equal to what the briefing's old
  per-hit ``query_facts(has_importance)`` + ``regenerate`` lookup returned;
* ``read_diary``, ``query_kg`` and ``list_drawers`` over the one-fold snapshot
  are identical to the store-surface path (forced via ``fold=None``);
* read paths no longer append duplicate scope / anchor cells to the log;
* the incremental cell-ref memo only hashes new events, and rebuilds when the
  log prefix changes.
"""

from __future__ import annotations

import pytest

pytest.importorskip("amplifier_data")

from amplifier_data.models import CellWriteEvent  # noqa: E402

from amplifier_module_tool_memory.store import (  # noqa: E402
    NativeMemoryStore,
    _CellRefCache,
)

from .test_search_no_regenerate import _build_synthetic_store, _force_old_path  # noqa: E402


def _expected_importance(store: NativeMemoryStore, ref) -> float | None:  # noqa: ANN001
    res = store.store.query_facts(subject=ref, predicate="has_importance")
    if not res.output:
        return None
    raw = store.store.regenerate(res.output[0].object, record_access=False).payload
    return float(raw.decode("utf-8"))


class TestSearchImportance:
    def test_hits_carry_importance_matching_fact_lookup(self) -> None:
        store = _build_synthetic_store()
        hits = store.search([1.0, 0.0, 0.0], 5, wing="w", lexical_query="auth")
        assert hits
        for hit in hits:
            assert "importance" in hit
            assert hit["importance"] == _expected_importance(store, hit["ref"])
        by_content = {h["content"]: h["importance"] for h in hits}
        assert by_content["we decided on the auth manifest approach"] == 0.9

    def test_importance_same_on_old_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        new = store.search([0.8, 0.6, 0.0], 5, wing="w", lexical_query="cache")
        _force_old_path(store, monkeypatch)
        old = store.search([0.8, 0.6, 0.0], 5, wing="w", lexical_query="cache")
        assert new == old


class TestFoldReadsMatchStoreSurface:
    def test_read_diary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        for i in range(4):
            store.file_diary(
                agent_name="curator", entry=f"entry {i}", topic=f"t{i % 2}"
            )
        new = store.read_diary(agent_name="curator", last_n=3)
        missing = store.read_diary(agent_name="nobody", last_n=3)
        _force_old_path(store, monkeypatch)
        assert new == store.read_diary(agent_name="curator", last_n=3)
        assert missing == store.read_diary(agent_name="nobody", last_n=3) == []
        assert [e["entry"] for e in new] == ["entry 1", "entry 2", "entry 3"]

    def test_query_kg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        store.assert_kg("alice", "likes", "tea")
        store.assert_kg("bob", "works_on", "auth")
        store.invalidate_kg("alice", "likes", "tea")
        new = [
            store.query_kg("alice"),
            store.query_kg(None, "works_on"),
            store.query_kg(),
        ]
        _force_old_path(store, monkeypatch)
        old = [
            store.query_kg("alice"),
            store.query_kg(None, "works_on"),
            store.query_kg(),
        ]
        assert new == old
        assert new[0] == [("alice", "works_on", "auth")]

    def test_list_drawers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        new = [
            store.list_drawers(wing="w"),
            store.list_drawers(room="other"),
            store.list_drawers(),
        ]

        # Old path: per-drawer regenerate + store-surface lenses over s.kernel.
        monkeypatch.setattr(store, "_fold_snapshot", lambda: None)
        old = [
            store.list_drawers(wing="w"),
            store.list_drawers(room="other"),
            store.list_drawers(),
        ]
        assert new == old

    def test_degraded_lexical_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        new = store.search(None, 5, wing="w", lexical_query="auth")
        _force_old_path(store, monkeypatch)
        assert new == store.search(None, 5, wing="w", lexical_query="auth")


class TestReadsDoNotAppend:
    def test_search_diary_kg_leave_log_unchanged(self) -> None:
        store = _build_synthetic_store()
        before = len(store.store.kernel.all_events())
        store.search([1.0, 0.0, 0.0], 3, wing="w", lexical_query="auth")
        store.search([1.0, 0.0, 0.0], 3, room="r", lexical_query="auth")
        store.search(None, 3, wing="never-filed", lexical_query="auth")
        store.read_diary(agent_name="curator", last_n=3)
        store.query_kg("alice")
        store.kg_timeline("alice")
        store.list_drawers(wing="w")
        assert len(store.store.kernel.all_events()) == before

    def test_read_ref_equals_write_cell_ref(self) -> None:
        store = NativeMemoryStore(record_access=False)
        for payload in (b"wing:w", b"entity:alice", b"agent:curator"):
            assert store._read_ref(payload) == store.store.write_cell(payload)

    def test_unknown_scope_still_empty(self) -> None:
        store = _build_synthetic_store()
        assert store.search([1.0, 0.0, 0.0], 3, wing="nope", lexical_query="x") == []
        assert store.read_diary(agent_name="nope") == []
        assert store.query_kg("nope") == []


class TestCellRefCache:
    def test_matches_uncached_and_hashes_only_new_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        kernel = store.store.kernel
        cache = _CellRefCache()
        events = kernel.all_events()
        expected = [
            ev.cell_ref() if isinstance(ev, CellWriteEvent) else None
            for _, ev in events
        ]
        assert cache.refs_for(events) == expected

        store.store.write_cell(b"brand new cell")
        calls = {"n": 0}
        orig = CellWriteEvent.cell_ref

        def counting(self):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return orig(self)

        monkeypatch.setattr(CellWriteEvent, "cell_ref", counting)
        events2 = kernel.all_events()
        refs2 = cache.refs_for(events2)
        assert calls["n"] == 1  # only the appended event was hashed
        assert refs2[: len(expected)] == expected
        assert refs2[-1] == orig(events2[-1][1])

    def test_rebuilds_when_prefix_changes(self) -> None:
        a = _build_synthetic_store().store.kernel.all_events()
        b = NativeMemoryStore(record_access=False)
        b.store.write_cell(b"other log entirely")
        other = b.store.kernel.all_events()
        cache = _CellRefCache()
        cache.refs_for(a)
        refs = cache.refs_for(other)  # shorter log -> rebuilt, not reused
        assert refs == [
            ev.cell_ref() if isinstance(ev, CellWriteEvent) else None for _, ev in other
        ]


class TestSnapshotReuse:
    def test_burst_of_reads_materializes_log_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_tool_memory.store as store_mod

        store = _build_synthetic_store()
        calls = {"n": 0}

        class _Counting(store_mod._SearchFold):
            def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
                calls["n"] += 1
                super().__init__(*a, **kw)

        monkeypatch.setattr(store_mod, "_SearchFold", _Counting)
        store.search([1.0, 0.0, 0.0], 3, wing="w", lexical_query="auth")
        store.read_diary(agent_name="curator", last_n=3)
        store.query_kg("alice")
        assert calls["n"] == 1

    def test_append_invalidates_snapshot(self) -> None:
        store = _build_synthetic_store()
        first = store._fold_snapshot()
        assert store._fold_snapshot() is first
        ref = store.file(
            wing="w", room="r", content="freshly filed", embedding=[0.0, 0.7, 0.7]
        )
        second = store._fold_snapshot()
        assert second is not first
        assert ref in second.payloads
        hits = store.search([0.0, 0.7, 0.7], 1, wing="w", lexical_query="freshly")
        assert hits[0]["ref"] == ref

    def test_snapshot_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _build_synthetic_store()
        monkeypatch.setattr(store, "SNAPSHOT_REUSE_S", 0.0)
        assert store._fold_snapshot() is not store._fold_snapshot()


class TestSnapshotRetentionBounded:
    """Review MUST-FIX 1: expiry must not pin every fold for SNAPSHOT_REUSE_S.

    A per-snapshot ``threading.Timer`` holding the fold kept one ~300-500 MB
    fold alive per write+read pair for 5 s (3.2 GB peak RSS on the real
    store). At most ONE fold may stay reachable from the store, and at most
    one expiry timer may be pending, however writes and reads interleave.
    """

    @staticmethod
    def _live(refs: list) -> int:  # noqa: ANN001
        import gc

        gc.collect()
        return sum(1 for r in refs if r() is not None)

    def test_interleaved_writes_and_reads_keep_one_fold(self) -> None:
        import threading
        import weakref

        store = _build_synthetic_store()
        assert store.SNAPSHOT_REUSE_S >= 1.0  # the window stays open all loop
        timers_before = sum(
            isinstance(t, threading.Timer) for t in threading.enumerate()
        )
        refs = []
        for i in range(8):
            store.file(
                wing="w", room="r", content=f"pair {i}", embedding=[0.5, 0.5, 0.0]
            )
            refs.append(weakref.ref(store._fold_snapshot()))
            store.search([0.5, 0.5, 0.0], 2, wing="w", lexical_query="pair")
        assert self._live(refs) <= 1
        timers_after = sum(
            isinstance(t, threading.Timer) for t in threading.enumerate()
        )
        assert timers_after - timers_before <= 1

    def test_snapshot_released_after_idle_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time
        import weakref

        store = _build_synthetic_store()
        monkeypatch.setattr(store, "SNAPSHOT_REUSE_S", 0.05)
        ref = weakref.ref(store._fold_snapshot())
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and (
            store._snapshot is not None or store._expiry_timer is not None
        ):
            time.sleep(0.02)
        assert store._snapshot is None
        assert store._expiry_timer is None
        assert self._live([ref]) == 0

    def test_timer_does_not_pin_the_store(self) -> None:
        import weakref

        store = _build_synthetic_store()
        store._fold_snapshot()
        assert store._expiry_timer is not None
        sref = weakref.ref(store)
        del store
        assert self._live([sref]) == 0

    def test_snapshot_built_across_an_append_is_not_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        store._fold_snapshot()  # subscribe the observer
        import amplifier_module_tool_memory.store as store_mod

        orig = store_mod._SearchFold

        def racing(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            fold = orig(*a, **kw)
            store._on_append(None, None)  # an append lands mid-build
            return fold

        store._on_append(None, None)  # invalidate the current slot
        monkeypatch.setattr(store_mod, "_SearchFold", racing)
        store._fold_snapshot()
        assert store._snapshot is None
