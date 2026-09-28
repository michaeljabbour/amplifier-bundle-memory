"""T1.2/T1.3/T1.4 -- RRF fusion, temporal filter, and the hot-path latency
gate on ``NativeMemoryStore.search`` (design \u00a72/\u00a76, docs/plans/2026-09-27-
memory-layers-design.md; D9, project-context/PROVENANCE.md).

Skipped entirely when amplifier-data is not installed, or when its
``lenses.bm25.BM25Index`` is not importable (an amplifier-data pin that
predates T1.1) -- same optional-dependency convention as the rest of this
suite.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

pytest.importorskip("amplifier_data")
bm25 = pytest.importorskip("amplifier_data.lenses.bm25")

from amplifier_module_tool_memory import store as store_mod  # noqa: E402
from amplifier_module_tool_memory.store import NativeMemoryStore  # noqa: E402


def _file(store: NativeMemoryStore, **kw: Any) -> Any:
    return store.file(**kw)


def _file_undated(
    store: NativeMemoryStore, *, wing: str, room: str, content: str
) -> Any:
    """A drawer with scope edges but NO ``filed_at`` fact -- simulates a
    pre-T0.2 legacy drawer, bypassing :meth:`NativeMemoryStore.file` (which
    always asserts ``filed_at``)."""
    s = store.store
    ref = s.write_cell(content.encode("utf-8"))
    s.scope(ref, s.write_cell(f"wing:{wing}".encode()))
    s.scope(ref, s.write_cell(f"room:{room}".encode()))
    return ref


class TestIdentifierRecovery:
    """The T1.2 acceptance case: a doc containing an exact identifier with
    no semantic neighbour is recovered by RRF but missed by legacy."""

    def _build(self) -> tuple[NativeMemoryStore, Any]:
        store = NativeMemoryStore(record_access=False)
        # Query vector points along the first axis; the target drawer's
        # embedding is ORTHOGONAL (no semantic overlap at all) -- only its
        # exact-identifier text can recover it.
        target = _file(
            store,
            wing="w",
            room="r",
            content="occurs due to ERR_4417 in the queue processor",
            embedding=[0.0, 0.0, 1.0],
        )
        # Ten distractors: high cosine similarity to the query, zero lexical
        # overlap with the identifier.
        for i in range(10):
            _file(
                store,
                wing="w",
                room="r",
                content=f"distractor note number {i} about unrelated topics",
                embedding=[1.0, float(i) * 1e-6, 0.0],
            )
        return store, target

    @pytest.mark.parametrize("k", [1, 2, 3])
    def test_legacy_misses_rrf_recovers(self, k: int) -> None:
        store, target = self._build()
        query_vector = [1.0, 0.0, 0.0]
        query_text = "what does ERR_4417 mean"

        legacy = store.search(
            query_vector,
            k,
            wing="w",
            room="r",
            lexical_query=query_text,
            fusion="legacy",
        )
        assert target not in {r["ref"] for r in legacy}, (
            "legacy (v2.0.1) formula was expected to miss the orthogonal "
            "identifier hit -- if this now passes, the fixture no longer "
            "reproduces the miss this test guards against"
        )

        rrf = store.search(
            query_vector, k, wing="w", room="r", lexical_query=query_text, fusion="rrf"
        )
        refs = {r["ref"] for r in rrf}
        assert target in refs
        hit = next(r for r in rrf if r["ref"] == target)
        assert hit["arms"]["bm25"] == 1


class TestScoreSemanticsPreserved:
    def test_blended_score_matches_legacy_formula_for_both_arm_hits(self) -> None:
        from amplifier_module_tool_memory.embedder import lexical_score

        store = NativeMemoryStore(record_access=False)
        ref = _file(
            store,
            wing="w",
            room="r",
            content="the auth manifest decision from last week",
            embedding=[1.0, 0.0, 0.0],
        )
        _file(
            store,
            wing="w",
            room="r",
            content="totally unrelated cooking content",
            embedding=[0.0, 1.0, 0.0],
        )

        results = store.search(
            [1.0, 0.0, 0.0],
            5,
            wing="w",
            room="r",
            lexical_query="auth manifest",
            fusion="rrf",
        )
        hit = next(r for r in results if r["ref"] == ref)
        assert hit["arms"]["semantic"] is not None
        assert hit["arms"]["bm25"] is not None
        expected = 0.85 * 1.0 + 0.15 * lexical_score("auth manifest", hit["content"])
        assert hit["score"] == pytest.approx(expected)


class TestScopeIsolation:
    def test_rrf_respects_wing_room_scope(self) -> None:
        store = NativeMemoryStore(record_access=False)
        in_scope = _file(
            store,
            wing="w1",
            room="r1",
            content="shared keyword alpha in w1",
            embedding=[1.0, 0.0],
        )
        out_of_scope = _file(
            store,
            wing="w2",
            room="r2",
            content="shared keyword alpha in w2",
            embedding=[1.0, 0.0],
        )
        assert in_scope != out_of_scope  # distinct content-addressed cells

        results = store.search(
            [1.0, 0.0], 10, wing="w1", room="r1", lexical_query="alpha", fusion="rrf"
        )
        refs = {r["ref"] for r in results}
        assert in_scope in refs
        assert out_of_scope not in refs


class TestIncrementalIndex:
    def test_new_drawers_found_no_retokenization_of_old_refs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = NativeMemoryStore(record_access=False)
        for i in range(3):
            _file(
                store,
                wing="w",
                room="r",
                content=f"original drawer {i} about foo",
                embedding=[1.0, float(i)],
            )

        calls: list[str] = []
        real_tokenize = bm25.tokenize

        def _spy(text: str) -> list[str]:
            calls.append(text)
            return real_tokenize(text)

        monkeypatch.setattr(bm25, "tokenize", _spy)

        store.search(
            [1.0, 0.0], 5, wing="w", room="r", lexical_query="foo", fusion="rrf"
        )
        first_pass_calls = len(calls)
        assert first_pass_calls > 0

        calls.clear()
        new_ref = _file(
            store,
            wing="w",
            room="r",
            content="brand new drawer about foo",
            embedding=[1.0, 3.0],
        )
        results = store.search(
            [1.0, 0.0], 5, wing="w", room="r", lexical_query="foo", fusion="rrf"
        )
        assert new_ref in {r["ref"] for r in results}
        # Only the ONE new drawer's text (+ the query text, once) should
        # have been tokenized -- the three original drawers must not be
        # re-tokenized on the second search.
        assert len(calls) == 2, (
            f"expected 2 tokenize() calls (1 doc + 1 query), got {calls}"
        )


class TestTemporalFilter:
    def test_since_until_excludes_out_of_range_and_undated(self) -> None:
        store = NativeMemoryStore(record_access=False)
        early = _file(
            store,
            wing="w",
            room="r",
            content="early note about widgets",
            filed_at="2020-01-01T00:00:00",
        )
        mid = _file(
            store,
            wing="w",
            room="r",
            content="mid note about widgets",
            filed_at="2020-06-01T00:00:00",
        )
        late = _file(
            store,
            wing="w",
            room="r",
            content="late note about widgets",
            filed_at="2021-01-01T00:00:00",
        )
        undated = _file_undated(
            store, wing="w", room="r", content="undated note about widgets"
        )

        results = store.search(
            None,
            10,
            wing="w",
            room="r",
            lexical_query="widgets",
            since="2020-01-01T00:00:00",
            until="2020-12-31T23:59:59",
        )
        refs = {r["ref"] for r in results}
        assert early in refs
        assert mid in refs
        assert late not in refs
        assert undated not in refs

    def test_no_bounds_is_a_no_op(self) -> None:
        store = NativeMemoryStore(record_access=False)
        undated = _file_undated(
            store, wing="w", room="r", content="undated note about widgets"
        )
        results = store.search(None, 10, wing="w", room="r", lexical_query="widgets")
        assert undated in {r["ref"] for r in results}


class TestBM25UnavailableFallback:
    def test_missing_bm25index_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = NativeMemoryStore(record_access=False)
        _file(store, wing="w", room="r", content="alpha content", embedding=[1.0, 0.0])
        _file(store, wing="w", room="r", content="beta content", embedding=[0.0, 1.0])

        monkeypatch.setattr(store_mod, "BM25Index", None)
        store._bm25_index = None

        default = store.search([1.0, 0.0], 5, wing="w", room="r", lexical_query="alpha")
        legacy = store.search(
            [1.0, 0.0], 5, wing="w", room="r", lexical_query="alpha", fusion="legacy"
        )
        assert default == legacy


class TestLatencyGate:
    """T1.4: p95 search latency on a 5k-drawer synthetic store."""

    @staticmethod
    def _build_synthetic_store(n: int = 5000) -> NativeMemoryStore:
        store = NativeMemoryStore(record_access=False)
        topics = [
            "auth",
            "cache",
            "deploy",
            "billing",
            "search",
            "widgets",
            "queue",
            "config",
        ]
        for i in range(n):
            topic = topics[i % len(topics)]
            # Small deterministic embedding derived from the index -- cheap
            # to generate, varied enough for a non-degenerate cosine ranking.
            vec = [
                float((i * 7) % 97) / 97.0,
                float((i * 13) % 89) / 89.0,
                float((i * 31) % 83) / 83.0,
            ]
            _file(
                store,
                wing="perf",
                room=topic,
                content=f"drawer {i} discussing {topic} decision number {i}",
                embedding=vec,
            )
        return store

    def test_p95_under_budget_both_fusion_modes(self) -> None:
        store = self._build_synthetic_store(5000)
        query_vector = [0.5, 0.5, 0.5]

        report: dict[str, float] = {}
        for fusion in ("rrf", "legacy"):
            latencies_ms: list[float] = []
            for i in range(30):
                start = time.perf_counter()
                store.search(
                    query_vector,
                    5,
                    wing="perf",
                    room="auth",
                    lexical_query=f"decision number {i}",
                    fusion=fusion,
                )
                latencies_ms.append((time.perf_counter() - start) * 1000.0)
            p95 = statistics.quantiles(latencies_ms, n=100)[94]
            report[fusion] = p95

        print(f"\nT1.4 search p95 (5k drawers, 30 queries): {report}")
        for fusion, p95 in report.items():
            assert p95 < store_mod.SEARCH_P95_BUDGET_MS, (
                f"fusion={fusion} p95={p95:.1f}ms exceeds budget "
                f"{store_mod.SEARCH_P95_BUDGET_MS}ms"
            )
