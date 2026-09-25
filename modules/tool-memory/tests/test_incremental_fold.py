"""Equivalence tests for perf/incremental-fold.

Two independent optimizations, each proven against a "fresh full fold" or
the pure-Python reference implementation it accelerates:

1. Incremental ``_SearchFold`` extension: when the store's kernel grows by
   pure appends, the NEXT fold reuses the PRIOR fold's payload join,
   per-subject index, and embedding-edge index instead of rescanning the
   whole log. Proven by interleaving writes and searches and comparing
   every observable surface (payloads, for_subject, embedding_edges,
   search results) against a FRESH fold built with ``prior=None`` over the
   same kernel state.
2. ``_fast_vector_query`` (numpy): proven against ``VectorLens.query()``'s
   pure-Python reference across randomized embeddings, k values, and scope
   filters -- same top-k ref set, closely matching scores (float64 numpy
   vs Python's native float64, differing only in summation order).

Skipped entirely when amplifier-data is not installed.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (  # noqa: E402
    NativeMemoryStore,
    _fast_vector_query,
    _SearchFold,
)

from .test_search_no_regenerate import _build_synthetic_store  # noqa: E402


def _fresh_full_fold(store: NativeMemoryStore) -> _SearchFold:
    """A fold built with NO prior -- always a full rebuild, the ground truth
    an incrementally-extended fold must match."""
    return _SearchFold(store.store.kernel, store._ref_cache)  # noqa: SLF001


class TestIsPrefixOf:
    def test_empty_fold_prefixes_anything(self) -> None:
        store = NativeMemoryStore(record_access=False)
        empty = _SearchFold(store.store.kernel, store._ref_cache)  # noqa: SLF001
        store.file(wing="w", room="r", content="one")
        grown = store.store.kernel.all_events()
        assert empty._is_prefix_of(grown)  # noqa: SLF001

    def test_true_append_only_extension_is_a_prefix(self) -> None:
        store = NativeMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="one")
        fold_a = _fresh_full_fold(store)
        store.file(wing="w", room="r", content="two")
        events_b = store.store.kernel.all_events()
        assert fold_a._is_prefix_of(events_b)  # noqa: SLF001

    def test_shorter_events_list_is_not_a_prefix(self) -> None:
        store = NativeMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="one")
        store.file(wing="w", room="r", content="two")
        fold_ab = _fresh_full_fold(store)
        events_a_only = store.store.kernel.all_events()[:1]
        assert not fold_ab._is_prefix_of(events_a_only)  # noqa: SLF001

    def test_diverged_longer_tail_is_not_a_prefix(self) -> None:
        """A prior fold that is LONGER than the candidate events can never
        be a valid extension base."""
        store = NativeMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="one")
        store.file(wing="w", room="r", content="two")
        fold_ab = _fresh_full_fold(store)
        events_a_only = store.store.kernel.all_events()[:1]
        assert not fold_ab._is_prefix_of(events_a_only)  # noqa: SLF001

    def test_same_length_check_is_seqpos_identity_not_content(self) -> None:
        """Documents an accepted, pre-existing limitation shared with
        ``_CellRefCache`` (same technique, same tradeoff, perf/startup-
        latency): the O(1) safety check compares length + the SeqPos at
        the boundary, not event content. Two INDEPENDENT in-memory kernels
        that each start their own SeqPos numbering from 0 can collide on
        that check. This is only reachable by literally swapping the
        kernel a store's fold is validated against mid-flight -- normal
        operation (one kernel, appended to over time) never hits it,
        which is why the existing cache accepted the same tradeoff.
        """
        store = NativeMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="one")
        fold_a = _fresh_full_fold(store)
        other_store = NativeMemoryStore(record_access=False)
        other_store.file(wing="w", room="r", content="different one")
        other_events = other_store.store.kernel.all_events()
        # Not asserted either way -- this documents behavior, it does not
        # prescribe it. The real guarantee (append-only continuation of
        # THE SAME kernel) is what test_true_append_only_extension_is_a_prefix
        # and the interleaved-writes equivalence tests below pin.
        fold_a._is_prefix_of(other_events)  # noqa: SLF001


class TestIncrementalFoldEquivalence:
    """Interleave writes and searches; every incrementally-extended fold
    must match a fresh full fold built over the SAME final kernel state."""

    def test_payloads_match_after_interleaved_writes(self) -> None:
        store = NativeMemoryStore(record_access=False)
        prior = None
        for i in range(12):
            store.file(wing="w", room="r", content=f"drawer {i}")
            fold = _SearchFold(store.store.kernel, store._ref_cache, prior=prior)  # noqa: SLF001
            prior = fold
        full = _fresh_full_fold(store)
        assert prior is not None
        assert prior.payloads == full.payloads
        assert prior.refs == full.refs

    def test_for_subject_matches_after_interleaved_writes_and_reads(self) -> None:
        store = NativeMemoryStore(record_access=False)
        prior = None
        refs = []
        for i in range(10):
            ref = store.file(wing="w", room="r", content=f"drawer {i}")
            refs.append(ref)
            fold = _SearchFold(store.store.kernel, store._ref_cache, prior=prior)  # noqa: SLF001
            # Force _by_subject to materialize on THIS fold before the next
            # extension, exercising the incremental (not lazy-fallback) path.
            fold.for_subject(ref)
            prior = fold
        full = _fresh_full_fold(store)
        for ref in refs:
            got = [pos for pos, _ev in prior.for_subject(ref).all_events()]  # type: ignore[union-attr]
            want = [pos for pos, _ev in full.for_subject(ref).all_events()]
            assert got == want

    def test_embedding_edges_match_after_interleaved_writes(self) -> None:
        store = NativeMemoryStore(record_access=False)
        prior = None
        for i in range(8):
            ref = store.file(wing="w", room="r", content=f"drawer {i}")
            store.store.add_embedding(ref, [float(i), 0.0, 1.0])  # type: ignore[attr-defined]
            fold = _SearchFold(store.store.kernel, store._ref_cache, prior=prior)  # noqa: SLF001
            fold.embedding_edges()  # force materialization before next extend
            prior = fold
        full = _fresh_full_fold(store)
        assert prior is not None
        assert sorted(prior.embedding_edges()) == sorted(full.embedding_edges())

    def test_vector_matrix_matches_after_interleaved_writes(self) -> None:
        store = NativeMemoryStore(record_access=False)
        prior = None
        for i in range(8):
            ref = store.file(wing="w", room="r", content=f"drawer {i}")
            store.store.add_embedding(ref, [float(i), 1.0, 0.0])  # type: ignore[attr-defined]
            fold = _SearchFold(store.store.kernel, store._ref_cache, prior=prior)  # noqa: SLF001
            fold.vector_matrix()  # force materialization before next extend
            prior = fold
        full = _fresh_full_fold(store)
        assert prior is not None
        got_refs, got_mat = prior.vector_matrix()  # type: ignore[misc]
        want_refs, want_mat = full.vector_matrix()  # type: ignore[misc]
        assert sorted(got_refs) == sorted(want_refs)
        got_by_ref = dict(zip(got_refs, got_mat.tolist()))
        want_by_ref = dict(zip(want_refs, want_mat.tolist()))
        for ref in want_by_ref:
            assert got_by_ref[ref] == pytest.approx(want_by_ref[ref])

    def test_search_results_match_a_fresh_full_fold_across_many_cycles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end contract: NativeMemoryStore.search(), called
        repeatedly while writes keep landing (the realistic production
        pattern -- a capture hook filing after every tool call), returns
        the SAME results an equivalent from-scratch rebuild would."""
        store = NativeMemoryStore(record_access=False)
        rng = random.Random(7)
        for cycle in range(6):
            for i in range(3):
                ref = store.file(
                    wing="w", room="r", content=f"cycle {cycle} drawer {i}"
                )
                store.store.add_embedding(  # type: ignore[attr-defined]
                    ref, [rng.random(), rng.random(), rng.random()]
                )
            incremental = store.search(
                [0.5, 0.5, 0.5], 5, wing="w", room="r", lexical_query="drawer"
            )
            # Force a fresh full fold for comparison (bypass the incremental
            # base without mutating the store's real prior_fold/snapshot).
            monkeypatch.setattr(store, "_prior_fold", None)
            monkeypatch.setattr(store, "_snapshot", None)
            fresh = store.search(
                [0.5, 0.5, 0.5], 5, wing="w", room="r", lexical_query="drawer"
            )
            assert [r["ref"] for r in incremental] == [r["ref"] for r in fresh]
            for a, b in zip(incremental, fresh):
                assert a["score"] == pytest.approx(b["score"])
                assert a["content"] == b["content"]


class TestFastVectorQueryEquivalence:
    """``_fast_vector_query`` (numpy) vs ``VectorLens.query()`` (reference)."""

    def _random_store(self, n: int, dim: int, seed: int) -> NativeMemoryStore:
        rng = random.Random(seed)
        store = NativeMemoryStore(record_access=False)
        for i in range(n):
            ref = store.file(wing="w", room="r" if i % 2 else "other", content=f"d{i}")
            vec = [rng.random() for _ in range(dim)]
            store.store.add_embedding(ref, vec)  # type: ignore[attr-defined]
        return store

    def test_matches_reference_unscoped(self) -> None:
        from amplifier_data.lenses.vector import VectorLens

        store = self._random_store(50, 16, seed=1)
        fold = store._fold_snapshot()  # noqa: SLF001
        assert fold is not None
        query = [random.Random(2).random() for _ in range(16)]

        fast = _fast_vector_query(fold, query, 10, None)
        ref = VectorLens().query(
            kernel=fold.vector_view(), vector=query, k=10, scope=None
        ).output

        assert fast is not None
        assert [r for r, _ in fast] == [r for r, _ in ref]
        for (_, fs), (_, rs) in zip(fast, ref):
            assert fs == pytest.approx(rs, abs=1e-6)

    def test_matches_reference_scoped(self) -> None:
        from amplifier_data.lenses.vector import VectorLens

        store = self._random_store(40, 8, seed=3)
        scope_ref = store._read_scope_ref("room", "r")  # noqa: SLF001
        fold = store._fold_snapshot()  # noqa: SLF001
        assert fold is not None
        query = [random.Random(4).random() for _ in range(8)]

        fast = _fast_vector_query(fold, query, 8, scope_ref)
        ref = VectorLens().query(
            kernel=fold.vector_view(), vector=query, k=8, scope=scope_ref
        ).output

        assert fast is not None
        assert [r for r, _ in fast] == [r for r, _ in ref]

    def test_no_embeddings_returns_empty_not_none(self) -> None:
        store = NativeMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="no embedding here")
        fold = store._fold_snapshot()  # noqa: SLF001
        assert fold is not None
        assert _fast_vector_query(fold, [1.0, 0.0], 5, None) == []

    def test_numpy_unavailable_falls_back_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._random_store(5, 4, seed=5)
        fold = store._fold_snapshot()  # noqa: SLF001
        assert fold is not None

        import builtins

        real_import = builtins.__import__

        def _blocked_numpy(name: str, *args: object, **kwargs: object) -> object:
            if name == "numpy":
                raise ImportError("numpy blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_numpy)
        # Fresh fold so vector_matrix()'s memoization doesn't short-circuit.
        fold2 = _fresh_full_fold(store)
        assert fold2.vector_matrix() is None
        assert _fast_vector_query(fold2, [0.1, 0.2, 0.3, 0.4], 5, None) is None

    def test_mixed_embedding_dimensions_falls_back_to_none(self) -> None:
        store = NativeMemoryStore(record_access=False)
        ref1 = store.file(wing="w", room="r", content="dim3")
        store.store.add_embedding(ref1, [1.0, 0.0, 0.0])  # type: ignore[attr-defined]
        ref2 = store.file(wing="w", room="r", content="dim4")
        store.store.add_embedding(ref2, [1.0, 0.0, 0.0, 0.0])  # type: ignore[attr-defined]
        fold = store._fold_snapshot()  # noqa: SLF001
        assert fold is not None
        assert fold.vector_matrix() is None


class TestSearchStaysCorrectUnderIncrementalFold:
    """Sanity: the existing synthetic-store equivalence fixture, run through
    the NOW-incremental fold path, still matches the old regenerate path."""

    def test_synthetic_store_search_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from .test_search_no_regenerate import _force_old_path

        store = _build_synthetic_store()
        query = [1.0, 0.1, 0.0]
        new = store.search(query, 3, wing="w", room="r", lexical_query="auth manifest")
        _force_old_path(store, monkeypatch)
        old = store.search(query, 3, wing="w", room="r", lexical_query="auth manifest")
        assert [r["ref"] for r in new] == [r["ref"] for r in old]
        for a, b in zip(new, old):
            assert a["score"] == pytest.approx(b["score"], abs=1e-6)
            assert a["content"] == b["content"]
