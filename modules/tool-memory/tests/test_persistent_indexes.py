"""T7.1-real (D32) acceptance tests: persistent, write-through derived
indexes that NEVER read the log on the search hot path (not even
incrementally) once warm, plus batched drawer+passage writes (T7.2) and
the NaN/zero-norm embedding guard.

Root cause history:
* D31 (EXPERIMENT_JOURNAL 2026-09-29): idle expiry dropped the
  incremental-extension base, so a search after any 5s+ idle gap rebuilt
  every derived index from the ENTIRE log -- 384-460s on a 1 GB store.
  T7.1 fixed this by never dropping it.
* Real-scale measurement (this file's namesake campaign) showed the FIX
  still called ``kernel.all_events()`` on every search (a "free" tail
  read that stopped being free at 1 GB: ~0.5-3s per call) and lazily
  built each wing's BM25/vector partition on first touch. T7.1-real
  removes BOTH: writes apply directly to the persistent index
  (``_apply_write_through``/``_SearchFold.apply_tail_in_place``, no
  kernel read at all), an ``os.stat()`` of the durable log (not a log
  read) detects a genuinely EXTERNAL writer, and the daemon's warm-up
  eagerly builds every wing's partition
  (``NativeMemoryStore.eager_build_all_wings``).

Skipped entirely when amplifier-data is not installed.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (  # noqa: E402
    NativeMemoryStore,
    _is_finite_nonzero_row,
)

from .test_search_no_regenerate import _build_synthetic_store  # noqa: E402


class TestPersistentIndexNeverExpires:
    """Acceptance criterion 1: after an idle gap, a search performs ZERO
    ``kernel.all_events()`` calls and returns results identical to a
    fresh-fold search."""

    def test_idle_search_reads_the_log_zero_times(self) -> None:
        store = _build_synthetic_store()
        # Warm the persistent index once (mirrors the daemon's startup
        # warm-up thread).
        store._fold_snapshot()  # noqa: SLF001
        baseline = store.search(
            [1.0, 0.0, 0.0], 5, wing="w", lexical_query="auth", fusion="legacy"
        )

        time.sleep(0.2)  # an idle gap -- irrelevant now, nothing expires

        calls = {"n": 0}
        real_kernel = store.store._kernel  # type: ignore[attr-defined]

        class _CountingKernelProxy:
            def __getattr__(self, name):  # noqa: ANN001, ANN204
                return getattr(real_kernel, name)

            def all_events(self):
                calls["n"] += 1
                return real_kernel.all_events()

        store.store._kernel = _CountingKernelProxy()  # type: ignore[attr-defined]
        try:
            after_idle = store.search(
                [1.0, 0.0, 0.0], 5, wing="w", lexical_query="auth", fusion="legacy"
            )
        finally:
            store.store._kernel = real_kernel  # type: ignore[attr-defined]

        assert calls["n"] == 0, (
            "a warm search must never call kernel.all_events() -- "
            f"it was called {calls['n']} time(s)"
        )
        assert [h["ref"] for h in after_idle] == [h["ref"] for h in baseline]
        for a, b in zip(after_idle, baseline):
            assert a["score"] == pytest.approx(b["score"])
            assert a["content"] == b["content"]

    def test_persistent_fold_survives_repeated_idle_gaps(self) -> None:
        store = _build_synthetic_store()
        first = store._fold_snapshot()  # noqa: SLF001
        for _ in range(3):
            time.sleep(0.05)
            fold = store._fold_snapshot()  # noqa: SLF001
            assert fold is first  # no writes landed -- identical object


class TestWriteThroughParity:
    """Acceptance criterion 2: after a mixed sequence of writes, the
    write-through-maintained persistent index answers identically to a
    freshly built store over the same log (property-style, fixed seed)."""

    def test_mixed_write_sequence_matches_fresh_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import random

        rng = random.Random(20260929)
        store = NativeMemoryStore(record_access=False)
        refs: list[object] = []
        for i in range(30):
            op = rng.choice(["file", "fact", "kg", "diary", "importance"])
            if op == "file" or not refs:
                ref = store.file(
                    wing=rng.choice(["w1", "w2"]),
                    room=rng.choice(["r1", "r2"]),
                    content=f"drawer {i} "
                    + " ".join(
                        rng.choice(["auth", "cache", "deploy", "manifest"])
                        for _ in range(5)
                    ),
                    category=rng.choice(["decision", "note", None]),
                    importance=rng.choice([0.2, 0.9, None]),
                    embedding=[rng.random(), rng.random(), rng.random()],
                )
                refs.append(ref)
            elif op == "fact":
                store.fact_add(
                    text="the team prefers pytest for this repo",
                    fact_type="preference",
                    source_refs=[rng.choice(refs)],
                    wing="w1",
                )
            elif op == "kg":
                store.assert_kg(f"entity{i % 3}", "relates_to", f"entity{(i + 1) % 3}")
            elif op == "diary":
                store.file_diary(agent_name="curator", entry=f"entry {i}")
            elif op == "importance":
                store.update_importance(
                    rng.choice(refs),
                    old_importance=None,
                    new_importance=0.5,
                    provenance="test",
                    source_outcome="test",
                    confidence=0.8,
                )

        incremental_search = store.search(
            [0.5, 0.5, 0.5], 10, wing="w1", lexical_query="auth cache"
        )
        incremental_kg = store.query_kg(None, "relates_to")
        incremental_list = store.list_drawers(wing="w1")

        # Force a from-scratch fold and compare every read surface against
        # it -- this is what a "freshly built store over the same log"
        # would answer, without needing a second store/log copy.
        monkeypatch.setattr(store, "_prior_fold", None)
        fresh_search = store.search(
            [0.5, 0.5, 0.5], 10, wing="w1", lexical_query="auth cache"
        )
        fresh_kg = store.query_kg(None, "relates_to")
        fresh_list = store.list_drawers(wing="w1")

        assert [h["ref"] for h in incremental_search] == [
            h["ref"] for h in fresh_search
        ]
        for a, b in zip(incremental_search, fresh_search):
            assert a["score"] == pytest.approx(b["score"])
        assert incremental_kg == fresh_kg
        assert incremental_list == fresh_list


class TestExternalWriterSafety:
    """Acceptance criterion 3: an out-of-band append from a second store
    instance sharing the same durable file is detected via ``os.stat``
    (never a log read on the request thread), caught up in the
    BACKGROUND, and never blocks the request that discovered it."""

    def test_second_process_writes_detected_and_caught_up_without_blocking(
        self, tmp_path
    ) -> None:
        path = str(tmp_path / "shared.amplifier")
        writer_a = NativeMemoryStore(path=path, record_access=False)
        writer_a.file(wing="w", room="r", content="from writer a", embedding=[1.0, 0.0])
        writer_a._fold_snapshot()  # noqa: SLF001 -- warm writer_a's own index
        writer_a.close()

        reader = NativeMemoryStore(path=path, record_access=False)
        reader._fold_snapshot()  # noqa: SLF001 -- warm + establish the stat baseline
        first_hits = reader.search(None, 10, wing="w", lexical_query="writer")
        assert {h["content"] for h in first_hits} == {"from writer a"}

        # A second, independent store instance over the SAME durable path
        # (the "tests/benchmarks in another process" scenario) appends
        # without going through *reader*'s write-through at all.
        writer_b = NativeMemoryStore(path=path, record_access=False)
        writer_b.file(wing="w", room="r", content="from writer b", embedding=[0.0, 1.0])
        writer_b.close()

        t0 = time.perf_counter()
        hits = reader.search(None, 10, wing="w", lexical_query="writer")
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, (
            "a search that merely DETECTS an external write must never "
            f"block on catching up -- took {elapsed:.2f}s"
        )
        # writer_b's drawer may not be visible on the VERY FIRST search
        # that discovers the change (the catch-up runs in the
        # background) -- poll briefly for it to land.
        deadline = time.monotonic() + 5.0
        contents: set[str] = {h["content"] for h in hits}
        while "from writer b" not in contents and time.monotonic() < deadline:
            time.sleep(0.05)
            hits = reader.search(None, 10, wing="w", lexical_query="writer")
            contents = {h["content"] for h in hits}
        reader.close()
        assert contents == {"from writer a", "from writer b"}


class TestBatchedPassageWrites:
    """Acceptance criterion 5: T7.2 -- one append per drawer+passages."""

    def test_atomic_branch_writes_drawer_and_passages_in_one_append_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from amplifier_data.envelope import WriteBatch

        store = NativeMemoryStore(record_access=False)
        assert store._supports_atomic_update()  # noqa: SLF001

        long_content = "sentence about auth manifest decisions. " * 60
        assert len(long_content) >= store.passage_min_chars

        calls = {"n": 0}
        real_commit = WriteBatch.commit

        def _counting_commit(self):  # noqa: ANN001
            calls["n"] += 1
            return real_commit(self)

        monkeypatch.setattr(WriteBatch, "commit", _counting_commit)
        ref = store.file(wing="w", room="r", content=long_content)

        assert calls["n"] == 1, (
            "the drawer's own commit must be the ONLY commit -- passages "
            "must be staged on the SAME batch, not committed separately"
        )
        assert store.has_passages(ref)


class TestEagerWingBuild:
    """Acceptance criterion 3 (eager startup): every wing's partition is
    materialized without waiting for a search to touch it."""

    def test_eager_build_all_wings_covers_every_wing_written(self) -> None:
        store = NativeMemoryStore(record_access=False)
        for i in range(3):
            store.file(
                wing=f"w{i}",
                room="r",
                content=f"drawer {i} about deploy pipelines",
                embedding=[float(i), 1.0, 0.0],
            )
        built = store.eager_build_all_wings()
        assert built == 3
        fold = store._fold_snapshot()  # noqa: SLF001
        for i in range(3):
            wing_ref = store._read_scope_ref("wing", f"w{i}")  # noqa: SLF001
            assert wing_ref in fold._scope_matrix_states  # noqa: SLF001
            assert wing_ref in store._scope_bm25_indexes  # noqa: SLF001


class TestNaNGuard:
    """Acceptance criterion 5: a zero-norm or non-finite embedding must
    never enter a matrix, and query-time scores must stay finite."""

    def test_row_validator(self) -> None:
        assert _is_finite_nonzero_row((1.0, 0.0, 0.0))
        assert not _is_finite_nonzero_row((0.0, 0.0, 0.0))  # zero-norm
        assert not _is_finite_nonzero_row((float("nan"), 0.0))
        assert not _is_finite_nonzero_row((float("inf"), 0.0))
        assert not _is_finite_nonzero_row((float("-inf"), 0.0))

    def test_corrupt_embedding_excluded_from_matrix_and_scores_stay_finite(
        self,
    ) -> None:
        import math

        store = NativeMemoryStore(record_access=False)
        good_ref = store.file(
            wing="w", room="r", content="a healthy drawer", embedding=[1.0, 0.0, 0.0]
        )
        bad_ref = store.file(
            wing="w",
            room="r",
            content="a corrupt-embedding drawer",
            embedding=[float("nan"), 0.0, 0.0],
        )
        zero_ref = store.file(
            wing="w",
            room="r",
            content="a zero-vector drawer",
            embedding=[0.0, 0.0, 0.0],
        )

        fold = store._fold_snapshot()  # noqa: SLF001
        refs, matrix = fold.scoped_vector_matrix(
            store._read_scope_ref("wing", "w")  # noqa: SLF001
        )
        assert good_ref in refs
        assert bad_ref not in refs
        assert zero_ref not in refs

        # RRF (the real production default) is the numpy-accelerated path
        # this guard protects; legacy fusion's VectorLens fallback is a
        # separate, byte-for-byte v2.0.1-compatibility path out of scope
        # for this guard.
        hits = store.search(
            [1.0, 0.0, 0.0], 10, wing="w", lexical_query="drawer", fusion="rrf"
        )
        assert hits  # the good drawer still ranks
        for hit in hits:
            assert math.isfinite(hit["score"])
            assert math.isfinite(hit.get("rrf", 0.0))
