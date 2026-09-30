"""P8 (D34): "rank on passages, read on neighborhoods" -- passages_around,
search's expand param, and the write-through no-log-read contract.

Skipped entirely when amplifier-data is not installed (optional dependency,
same convention as the rest of this suite).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (
    DEFAULT_PASSAGE_MIN_CHARS,
    NativeMemoryStore,
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
        h = sum(ord(c) for c in text)
        return [
            float((h * 7) % 97) / 97.0,
            float((h * 13) % 89) / 89.0,
            float((h * 31) % 83) / 83.0,
        ]


def _long_content(marker: str = "", filler_lines: int = 40) -> str:
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


class TestPassagesAround:
    def test_small_drawer_returns_itself_as_one_item(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = "a short note"
        ref = store.file(wing="w", room="r", content=content, source="s")
        items = store.passages_around(ref)
        assert len(items) == 1
        assert items[0]["ref"] == str(ref)
        assert items[0]["span"] == [0, len(content)]
        assert items[0]["content"] == content

    def test_long_drawer_returns_every_passage_in_span_order_when_span_omitted(
        self,
    ) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="unique-marker-p8a")
        ref = store.file(wing="w", room="r", content=content, embedder=_FakeEmbedder())
        items = store.passages_around(ref)
        assert len(items) >= 2
        starts = [item["span"][0] for item in items]
        assert starts == sorted(starts), "must be in ascending span order"
        # Reconstructing content from spans must reproduce substrings of
        # the original (never fabricated text).
        for item in items:
            start, end = item["span"]
            assert content[start:end] == item["content"]

    def test_span_selects_the_covering_passage_and_before_after_neighbors(
        self,
    ) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="unique-marker-p8b")
        ref = store.file(wing="w", room="r", content=content, embedder=_FakeEmbedder())
        every = store.passages_around(ref)
        assert len(every) >= 3
        mid_idx = len(every) // 2
        mid_start = every[mid_idx]["span"][0]

        selected = store.passages_around(
            ref, span=[mid_start, mid_start + 1], before=1, after=1
        )
        assert len(selected) == min(3, len(every))
        # The center passage must be included.
        assert every[mid_idx]["ref"] in [s["ref"] for s in selected]

    def test_span_at_edge_clamps_before_after(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="unique-marker-p8c")
        ref = store.file(wing="w", room="r", content=content, embedder=_FakeEmbedder())
        every = store.passages_around(ref)
        first_start = every[0]["span"][0]
        selected = store.passages_around(
            ref, span=[first_start, first_start + 1], before=5, after=1
        )
        # Cannot go before index 0.
        assert selected[0]["ref"] == every[0]["ref"]

    def test_before_after_zero_returns_only_the_covering_passage(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="unique-marker-p8d")
        ref = store.file(wing="w", room="r", content=content, embedder=_FakeEmbedder())
        every = store.passages_around(ref)
        mid = every[len(every) // 2]
        start = mid["span"][0]
        selected = store.passages_around(
            ref, span=[start, start + 1], before=0, after=0
        )
        assert len(selected) == 1
        assert selected[0]["ref"] == mid["ref"]

    def test_no_log_read_on_the_passages_around_path(self, monkeypatch: Any) -> None:
        """T7.1/D33's write-through invariant, extended to P8: once the
        drawer's write-through fold has the passage payloads (as it does
        immediately after `file()`, same process), `passages_around` must
        resolve every passage's content WITHOUT a single `get_cell` kernel
        round trip."""
        store = NativeMemoryStore(record_access=False)
        content = _long_content(marker="unique-marker-p8e")
        ref = store.file(wing="w", room="r", content=content, embedder=_FakeEmbedder())

        calls = {"n": 0}
        real_get_cell = store.store.get_cell

        def _spy_get_cell(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real_get_cell(*args, **kwargs)

        monkeypatch.setattr(store.store, "get_cell", _spy_get_cell)
        items = store.passages_around(ref)
        assert len(items) >= 2
        assert calls["n"] == 0, "passages_around must not read the log"


class TestSearchExpand:
    @staticmethod
    def _file_long_drawer(
        store: NativeMemoryStore, marker: str, embedder: Any
    ) -> tuple[Any, str]:
        content = _long_content(marker=marker)
        ref = store.file(
            wing="w",
            room="r",
            content=content,
            source="orig-source",
            embedder=embedder,
        )
        return ref, content

    def test_expand_none_adds_no_context_field(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        _drawer_ref, _content = self._file_long_drawer(
            store, "unique-marker-exp0", embedder
        )
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-exp0",
            fusion="rrf",
            granularity="passage",
        )
        assert hits
        for h in hits:
            assert "context" not in h

    def test_expand_neighbors_merges_adjacent_passages(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        _drawer_ref, content = self._file_long_drawer(
            store, "unique-marker-exp1", embedder
        )
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-exp1",
            fusion="rrf",
            granularity="passage",
            expand="neighbors",
            expand_neighbors=1,
        )
        passage_hits = [h for h in hits if h.get("layer") == "passage"]
        assert passage_hits
        top = passage_hits[0]
        assert "context" in top
        # The merged context must be a real substring of the drawer, and
        # at least as large as the bare passage (it includes neighbors).
        assert top["context"] in content
        assert len(top["context"]) >= len(top["content"])

    def test_expand_drawer_returns_whole_parent_content(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        _drawer_ref, content = self._file_long_drawer(
            store, "unique-marker-exp2", embedder
        )
        hits = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-exp2",
            fusion="rrf",
            granularity="passage",
            expand="drawer",
        )
        passage_hits = [h for h in hits if h.get("layer") == "passage"]
        assert passage_hits
        assert passage_hits[0]["context"] == content

    def test_expand_does_not_change_rank_order_or_scores(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        self._file_long_drawer(store, "unique-marker-exp3", embedder)
        base = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-exp3",
            fusion="rrf",
            granularity="passage",
        )
        expanded = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="unique-marker-exp3",
            fusion="rrf",
            granularity="passage",
            expand="neighbors",
        )
        assert [h["ref"] for h in base] == [h["ref"] for h in expanded]
        assert [h.get("score") for h in base] == [h.get("score") for h in expanded]

    def test_expand_char_budget_limits_later_hits(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        # File several long drawers so several passage hits compete for a
        # tiny budget.
        for i in range(4):
            self._file_long_drawer(store, f"budget-marker-{i}-shared-term", embedder)
        hits = store.search(
            None,
            10,
            wing="w",
            room="r",
            lexical_query="shared-term",
            fusion="rrf",
            granularity="passage",
            expand="drawer",
            expand_char_budget=1,  # smaller than even one drawer
        )
        passage_hits = [h for h in hits if h.get("layer") == "passage"]
        assert passage_hits
        # The budget of 1 char is smaller than any single drawer, but the
        # FIRST expanded hit must never be rejected for being oversized.
        with_context = [h for h in passage_hits if "context" in h]
        assert len(with_context) == 1
        assert with_context[0] is passage_hits[0]

    def test_fact_hits_are_never_expanded(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        vec = embedder.embed("unique-marker-exp4 fact text")
        store.fact_add(
            text="unique-marker-exp4 fact text is durable",
            fact_type="world",
            source_refs=[
                store.file(
                    wing="w",
                    room="r",
                    content="unique-marker-exp4 source",
                    embedder=embedder,
                )
            ],
            wing="w",
            embedding=vec,
        )
        hits = store.search(
            vec,
            5,
            wing="w",
            lexical_query="unique-marker-exp4",
            fusion="rrf",
            layers=["fact"],
            expand="neighbors",
        )
        fact_hits = [h for h in hits if h.get("layer") == "fact"]
        assert fact_hits
        for h in fact_hits:
            assert "context" not in h
