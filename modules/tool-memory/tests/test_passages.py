"""T6.2 (D29): passage index -- splitting, search granularity, backfill,
rerank wiring (T6.4), and embedding-model-mismatch exclusion/requeue (T6.5).

Skipped entirely when amplifier-data is not installed (optional dependency,
same convention as the rest of this suite).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory import store as store_mod
from amplifier_module_tool_memory.store import (
    DEFAULT_PASSAGE_CHARS,
    DEFAULT_PASSAGE_MIN_CHARS,
    NativeMemoryStore,
    _split_into_passages,
)


class _FakeEmbedder:
    """Deterministic content-derived embedder -- no model download needed."""

    def __init__(self, *, model_id: str = "fake-model:3", ready: bool = True) -> None:
        self.model_id = model_id
        self._ready = ready

    @property
    def ready(self) -> bool:
        return self._ready

    def embed(self, text: str) -> list[float]:
        # Deterministic 3-dim vector derived from content hash so cosine
        # ranking is meaningful and reproducible.
        h = sum(ord(c) for c in text)
        return [
            float((h * 7) % 97) / 97.0,
            float((h * 13) % 89) / 89.0,
            float((h * 31) % 83) / 83.0,
        ]


def _long_content(marker: str = "", filler_lines: int = 40) -> str:
    """Deterministic long text (well above DEFAULT_PASSAGE_MIN_CHARS),
    built from distinct conversational-turn-shaped lines so the splitter's
    boundary detection has real boundaries to work with."""
    lines: list[str] = []
    for i in range(filler_lines):
        lines.append(f"user: this is filler turn number {i} about the deploy pipeline")
        lines.append(f"assistant: acknowledged turn {i}, nothing special here")
        lines.append("")
    if marker:
        lines.append(f"assistant: {marker}")
    text = "\n".join(lines)
    assert len(text) >= DEFAULT_PASSAGE_MIN_CHARS, (
        "test fixture must exceed the threshold"
    )
    return text


class TestSplitIntoPassages:
    def test_short_text_yields_one_passage_or_none_split_at_line_boundaries(
        self,
    ) -> None:
        text = "line one\nline two\nline three\n"
        passages = _split_into_passages(text, passage_chars=900, overlap=150)
        assert len(passages) == 1
        start, end, ptext = passages[0]
        assert (start, end, ptext) == (0, len(text), text)

    def test_long_text_splits_into_multiple_passages(self) -> None:
        text = _long_content()
        passages = _split_into_passages(
            text, passage_chars=DEFAULT_PASSAGE_CHARS, overlap=150
        )
        assert len(passages) >= 2
        # never splits mid-line: every passage starts/ends on a line boundary.
        lines_with_ends = set()
        pos = 0
        for line in text.splitlines(keepends=True):
            lines_with_ends.add(pos)
            pos += len(line)
        lines_with_ends.add(pos)
        for start, end, _ptext in passages:
            assert start in lines_with_ends
            assert end in lines_with_ends

    def test_consecutive_passages_overlap(self) -> None:
        text = _long_content()
        passages = _split_into_passages(
            text, passage_chars=DEFAULT_PASSAGE_CHARS, overlap=150
        )
        assert len(passages) >= 2
        for (_s0, e0, _t0), (s1, _e1, _t1) in zip(passages, passages[1:]):
            assert s1 < e0, "consecutive passages must overlap"

    def test_idempotent_same_text_same_passages(self) -> None:
        text = _long_content()
        a = _split_into_passages(text, passage_chars=DEFAULT_PASSAGE_CHARS, overlap=150)
        b = _split_into_passages(text, passage_chars=DEFAULT_PASSAGE_CHARS, overlap=150)
        assert a == b

    def test_empty_text_yields_no_passages(self) -> None:
        assert _split_into_passages("", passage_chars=900, overlap=150) == []


class TestWriteTimePassageCreation:
    def test_short_drawer_gets_no_passages(self) -> None:
        store = NativeMemoryStore(record_access=False)
        ref = store.file(wing="w", room="r", content="a short note", source="s")
        assert store.has_passages(ref) is False

    def test_long_drawer_gets_passages(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content()
        ref = store.file(wing="w", room="r", content=content, source="s")
        assert store.has_passages(ref) is True

    def test_refiling_identical_long_content_is_idempotent(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content()
        ref1 = store.file(wing="w", room="r", content=content, source="s")
        assert store.has_passages(ref1) is True
        # Re-filing identical content (content addressing) is the SAME ref,
        # and its passages remain intact.
        ref2 = store.file(wing="w", room="r", content=content, source="s")
        assert ref1 == ref2
        assert store.has_passages(ref2) is True

    def test_passage_embedded_inline_when_embedder_ready(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        content = _long_content()
        ref = store.file(
            wing="w", room="r", content=content, source="s", embedder=embedder
        )
        fold = store._fold_snapshot()
        assert fold is not None
        # Every passage of this drawer should carry `embedded_with`, not
        # `needs_embedding` (T6.5).
        s = store.store
        pending = s.query_facts(predicate="needs_embedding")
        pending_refs = {f.subject for f in pending.output}
        assert ref not in pending_refs


class TestSearchGranularity:
    @staticmethod
    def _file_long_drawer(store: NativeMemoryStore, marker: str, embedder: Any) -> Any:
        content = _long_content(marker=marker)
        return store.file(
            wing="w",
            room="r",
            content=content,
            source="orig-source",
            category="orig-category",
            embedder=embedder,
        ), content

    def test_passage_hit_carries_drawer_ref_span_and_attribution(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        drawer_ref, _content = self._file_long_drawer(
            store, "unique-marker-alpha", embedder
        )

        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-alpha",
            fusion="rrf",
            granularity="passage",
        )
        passage_hits = [h for h in hits if h.get("layer") == "passage"]
        assert passage_hits, f"expected a passage hit, got {hits}"
        hit = passage_hits[0]
        assert hit["drawer_ref"] == drawer_ref
        assert "span" in hit and len(hit["span"]) == 2
        assert hit["wing"] == "w"
        assert hit["room"] == "r"
        assert hit["source"] == "orig-source"
        assert hit["category"] == "orig-category"
        assert hit["filed_at"] is not None

    def test_drawer_mode_rolls_up_to_full_content(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        drawer_ref, content = self._file_long_drawer(
            store, "unique-marker-beta", embedder
        )

        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-beta",
            fusion="rrf",
            granularity="drawer",
        )
        drawer_hits = [h for h in hits if h["ref"] == drawer_ref]
        assert drawer_hits, f"expected the parent drawer as a hit, got {hits}"
        hit = drawer_hits[0]
        assert hit["layer"] == "drawer"
        assert hit["content"] == content  # full verbatim content, not a snippet

    def test_identifier_in_late_passage_recovered_at_rank_one(self) -> None:
        """A rare identifier buried near the END of a long drawer must
        rank FIRST in passage mode -- the exact case T6.2 exists for
        (D29's k=10-hands-back-mostly-noise diagnosis): the identifier's
        OWN passage, not the whole multi-thousand-char drawer, is what
        gets scored against the query."""
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        content = _long_content(marker="ERR_9987_UNIQUE_IDENTIFIER")
        ref = store.file(wing="w", room="r", content=content, embedder=embedder)
        assert store.has_passages(ref)

        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="ERR_9987_UNIQUE_IDENTIFIER",
            fusion="rrf",
            granularity="passage",
        )
        assert hits, "expected at least one hit"
        top = hits[0]
        assert top["layer"] == "passage"
        assert top["drawer_ref"] == ref
        assert "ERR_9987_UNIQUE_IDENTIFIER" in top["content"]
        # The recovered passage must be a SLICE, not the whole drawer.
        assert len(top["content"]) < len(content)

    def test_long_drawer_itself_is_not_double_reported_in_passage_mode(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        drawer_ref, _content = self._file_long_drawer(
            store, "unique-marker-gamma", embedder
        )

        hits = store.search(
            None,
            20,
            wing="w",
            room="r",
            lexical_query="unique-marker-gamma",
            fusion="rrf",
            granularity="passage",
        )
        # The raw drawer ref must never appear as its own hit in passage
        # mode -- its passages represent it instead.
        assert all(h["ref"] != drawer_ref for h in hits)

    def test_collapse_caps_passages_per_drawer(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        # A very long drawer with the SAME marker repeated in many passages
        # so multiple passages plausibly rank highly for one query.
        lines = []
        for i in range(80):
            lines.append(f"user: turn {i} discussing the shared-term everywhere")
            lines.append("assistant: ack")
            lines.append("")
        content = "\n".join(lines)
        ref = store.file(wing="w", room="r", content=content, embedder=embedder)
        assert store.has_passages(ref)

        hits = store.search(
            None,
            20,
            wing="w",
            room="r",
            lexical_query="shared-term",
            fusion="rrf",
            granularity="passage",
            max_passages_per_drawer=2,
        )
        from_this_drawer = [h for h in hits if h.get("drawer_ref") == ref]
        assert len(from_this_drawer) <= 2

    def test_short_drawers_unaffected_by_granularity(self) -> None:
        store = NativeMemoryStore(record_access=False)
        ref = store.file(wing="w", room="r", content="a short note about deploys")
        for granularity in ("passage", "drawer"):
            hits = store.search(
                None,
                5,
                wing="w",
                room="r",
                lexical_query="deploys",
                fusion="rrf",
                granularity=granularity,
            )
            matching = [h for h in hits if h["ref"] == ref]
            assert matching and matching[0]["layer"] == "drawer"


class TestPassageBackfill:
    def test_ensure_passages_creates_then_is_a_noop(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="backfill-marker")
        # Simulate a pre-existing drawer filed BEFORE this feature landed:
        # write it directly through the store, bypassing file()'s own
        # passage creation.
        s = store.store
        ref = s.write_cell(content.encode("utf-8"))
        s.scope(ref, s.write_cell(b"wing:w"))
        s.scope(ref, s.write_cell(b"room:r"))
        assert store.has_passages(ref) is False

        created = store.ensure_passages(ref, content, wing="w", room="r")
        assert created  # passages were created
        assert store.has_passages(ref) is True

        # Idempotent: calling again on an already-backfilled drawer is a no-op.
        created_again = store.ensure_passages(ref, content, wing="w", room="r")
        assert created_again == []

    def test_ensure_passages_noop_for_short_content(self) -> None:
        store = NativeMemoryStore(record_access=False)
        ref = store.file(wing="w", room="r", content="short")
        assert store.ensure_passages(ref, "short", wing="w", room="r") == []


class TestRerankWiring:
    def _long_store_with_hits(self) -> tuple[NativeMemoryStore, Any]:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        content = _long_content(marker="rerank-target-marker")
        ref = store.file(wing="w", room="r", content=content, embedder=embedder)
        return store, ref

    def test_rerank_off_by_default(self) -> None:
        store, _ref = self._long_store_with_hits()
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="rerank-target-marker",
            fusion="rrf",
        )
        assert hits
        assert all("rerank_score" not in h for h in hits)
        assert all("rerank_skipped" not in h for h in hits)

    def test_rerank_true_reorders_and_tags_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ref = self._long_store_with_hits()

        class _FakeReranker:
            model_id = "fake-reranker"

            def score(self, query: str, texts: list[str]) -> list[float]:
                # Reverse the input order's implied ranking so we can prove
                # reordering actually happened.
                return list(range(len(texts)))

        monkeypatch.setattr(store_mod, "get_reranker", lambda config: _FakeReranker())
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="rerank-target-marker",
            fusion="rrf",
            rerank=True,
        )
        assert hits
        assert all("rerank_score" in h for h in hits)
        scores = [h["rerank_score"] for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_rerank_none_falls_back_to_store_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ref = self._long_store_with_hits()
        store.rerank_mode = "cross-encoder"

        class _FakeReranker:
            model_id = "fake-reranker"

            def score(self, query: str, texts: list[str]) -> list[float]:
                return [1.0] * len(texts)

        monkeypatch.setattr(store_mod, "get_reranker", lambda config: _FakeReranker())
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="rerank-target-marker",
            fusion="rrf",
        )
        assert hits and all("rerank_score" in h for h in hits)

    def test_rerank_unavailable_skips_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ref = self._long_store_with_hits()
        monkeypatch.setattr(store_mod, "get_reranker", lambda config: None)
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="rerank-target-marker",
            fusion="rrf",
            rerank=True,
        )
        assert hits
        assert all("rerank_skipped" in h for h in hits)
        assert all("rerank_score" not in h for h in hits)

    def test_rerank_raising_skips_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ref = self._long_store_with_hits()

        class _RaisingReranker:
            model_id = "raises"

            def score(self, query: str, texts: list[str]) -> list[float]:
                raise RuntimeError("boom")

        monkeypatch.setattr(
            store_mod, "get_reranker", lambda config: _RaisingReranker()
        )
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="rerank-target-marker",
            fusion="rrf",
            rerank=True,
        )
        assert hits
        assert all("rerank_skipped" in h for h in hits)
        assert any("boom" in h["rerank_skipped"] for h in hits)


class TestEmbeddingModelMismatch:
    def test_mismatched_model_excluded_from_semantic_arm(self) -> None:
        store = NativeMemoryStore(record_access=False)
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [1.0, 0.0, 0.0]  # identical cosine target -- only model_id differs
        ref_current = store.file(
            wing="w",
            room="r",
            content="alpha note filed with the current model",
            embedding=vec_a,
            embedding_model_id="model-a",
        )
        ref_stale = store.file(
            wing="w",
            room="r",
            content="beta note filed with a stale model",
            embedding=vec_b,
            embedding_model_id="model-b",
        )

        hits = store.search(
            vec_a,
            5,
            wing="w",
            room="r",
            lexical_query="nonmatching query text zzz",
            fusion="rrf",
            current_model_id="model-a",
        )
        hit_refs = {h["ref"] for h in hits}
        assert ref_current in hit_refs
        assert ref_stale not in hit_refs

    def test_ref_with_no_recorded_model_id_is_always_current(self) -> None:
        store = NativeMemoryStore(record_access=False)
        vec = [0.0, 1.0, 0.0]
        ref = store.file(
            wing="w", room="r", content="legacy pre-T6.5 note", embedding=vec
        )
        hits = store.search(
            vec,
            5,
            wing="w",
            room="r",
            lexical_query="legacy",
            fusion="rrf",
            current_model_id="model-a",
        )
        assert any(h["ref"] == ref for h in hits)

    def test_requeue_stale_embeddings_marks_needs_embedding(self) -> None:
        store = NativeMemoryStore(record_access=False)
        vec = [1.0, 0.0, 0.0]
        ref_stale = store.file(
            wing="w",
            room="r",
            content="stale-model note",
            embedding=vec,
            embedding_model_id="model-old",
        )
        ref_current = store.file(
            wing="w",
            room="r",
            content="current-model note",
            embedding=vec,
            embedding_model_id="model-new",
        )

        requeued = store.requeue_stale_embeddings("model-new")
        assert requeued >= 1

        s = store.store
        pending = s.query_facts(predicate="needs_embedding")
        pending_refs = {f.subject for f in pending.output}
        assert ref_stale in pending_refs
        assert ref_current not in pending_refs
