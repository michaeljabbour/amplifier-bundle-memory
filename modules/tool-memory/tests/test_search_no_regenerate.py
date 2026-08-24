"""Result-equivalence proof for the search perf fix (perf/search-no-regenerate).

``NativeMemoryStore.search`` used to call ``store.regenerate(ref)`` for every
candidate (k*3) and again for every returned hit's content, scope labels, and
fact values, and re-materialized the whole event log for every store-surface
lens read. ``regenerate`` drops the materialized cache and re-folds the WHOLE
log per call, so search was O(reads x log) (~12s/query on a ~38k-event
store). The fix pays for ONE fold per search call (``_fold_snapshot`` ->
``_SearchFold``): the substrate's own lenses run over the cached event list
and payloads come from the fold's ``ref -> payload`` join.

These tests pin the contract of the fix:

* the snapshot returns byte-identical payloads to ``regenerate`` for every
  cell in the log (content addressing makes this exact, not approximate);
* search results (refs, scores, order, and every result field) are identical
  between the new one-fold path and the old per-read path, which is
  reproduced exactly by forcing the ``fold=None`` fallback;
* backends without a foldable kernel (remote stores) keep the old path.

Skipped entirely when amplifier-data is not installed (optional dependency,
same convention as test_native_store_reads.py).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (  # noqa: E402
    NativeMemoryStore,
)


def _build_synthetic_store() -> NativeMemoryStore:
    """A store exercising every field search touches: multiple wings/rooms,
    embeddings, categories, sources, importances, a needs_embedding pending
    drawer (lexical-union hardening path), and diary/KG cells as bystanders."""
    store = NativeMemoryStore(record_access=False)
    s = store.store

    vectors = {
        "auth": [1.0, 0.0, 0.0],
        "cache": [0.8, 0.6, 0.0],
        "cook": [0.0, 1.0, 0.0],
        "deploy": [0.0, 0.0, 1.0],
    }
    refs: dict[str, Any] = {}
    refs["auth"] = store.file(
        wing="w",
        room="r",
        content="we decided on the auth manifest approach",
        source="session-1",
        category="decision",
        importance=0.9,
        embedding=vectors["auth"],
    )
    refs["cache"] = store.file(
        wing="w",
        room="r",
        content="the cache invalidation strategy for auth tokens",
        source="session-2",
        category="learning",
        importance=0.5,
        embedding=vectors["cache"],
    )
    refs["cook"] = store.file(
        wing="w",
        room="r",
        content="unrelated notes about cooking pasta",
        embedding=vectors["cook"],
    )
    refs["deploy"] = store.file(
        wing="w",
        room="other",
        content="deploy checklist for the auth service",
        source="session-3",
        embedding=vectors["deploy"],
    )
    # A drawer filed before the embedder was ready: no vector, marked pending
    # -- reachable only via the lexical-union hardening inside search.
    refs["pending"] = store.file(
        wing="w", room="r", content="a pending auth drawer with no vector yet"
    )
    s.assert_fact(refs["pending"], "needs_embedding", s.write_cell(b"true"))
    # Bystander cells that must not perturb search: KG facts and a diary entry.
    store.assert_kg("alice", "works_on", "auth")
    store.file_diary(agent_name="curator", entry="looked at auth today", topic="r")
    return store


def _force_old_path(store: NativeMemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the pre-fix behavior exactly: with no snapshot, every payload
    read falls back to ``regenerate(record_access=False)`` and every lens read
    goes back through the store surface -- the same calls, with the same
    arguments, the old per-hit code made."""
    monkeypatch.setattr(store, "_fold_snapshot", lambda: None)


class TestSnapshotEquivalence:
    def test_snapshot_payloads_match_regenerate_bytes(self) -> None:
        store = _build_synthetic_store()
        fold = store._fold_snapshot()
        assert fold is not None
        assert fold.payloads  # non-empty: the fold saw the synthetic cells
        for ref, raw in fold.payloads.items():
            assert store.store.regenerate(ref, record_access=False).payload == raw, (
                f"snapshot payload diverges from regenerate for {ref}"
            )

    def test_no_kernel_backend_returns_none(self) -> None:
        class KernellessBackend:
            """Remote-shaped backend: no .kernel attribute at all."""

        store = NativeMemoryStore(store=KernellessBackend())
        assert store._fold_snapshot() is None


class TestSearchResultEquivalence:
    def test_vector_search_identical_to_old_regenerate_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        query = [1.0, 0.1, 0.0]
        new = store.search(query, 3, wing="w", room="r", lexical_query="auth manifest")
        _force_old_path(store, monkeypatch)
        old = store.search(query, 3, wing="w", room="r", lexical_query="auth manifest")
        assert new == old  # same refs, scores, ORDER, and every result field
        assert [r["ref"] for r in new] == [r["ref"] for r in old]

    def test_vector_search_unscoped_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        query = [0.0, 0.0, 1.0]
        new = store.search(query, 10, lexical_query="deploy auth")
        _force_old_path(store, monkeypatch)
        old = store.search(query, 10, lexical_query="deploy auth")
        assert new == old

    def test_pending_drawer_union_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        query = [1.0, 0.0, 0.0]
        new = store.search(query, 5, wing="w", room="r", lexical_query="auth")
        _force_old_path(store, monkeypatch)
        old = store.search(query, 5, wing="w", room="r", lexical_query="auth")
        assert new == old
        # the hardening path itself still works through the snapshot
        assert any("pending" in r["content"] for r in new)

    def test_degraded_lexical_only_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _build_synthetic_store()
        new = store.search(None, 5, wing="w", room="r", lexical_query="auth manifest")
        _force_old_path(store, monkeypatch)
        old = store.search(None, 5, wing="w", room="r", lexical_query="auth manifest")
        assert new == old
