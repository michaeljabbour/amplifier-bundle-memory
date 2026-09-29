"""T6.2 (D29): the daemon's bounded passage-backfill sweep
(`daemon._sweep_passage_backfill`) -- exercised directly against
`NativeMemoryStore` (no HTTP server needed; the sweep takes the same
`(mem_store, embedder, lock)` shape `_watch_embedder_and_sweep` does).

Skipped entirely when amplifier-data is not installed.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.daemon import _sweep_passage_backfill
from amplifier_module_tool_memory.store import NativeMemoryStore


class _FakeEmbedder:
    def __init__(self, *, model_id: str = "fake-model:3") -> None:
        self.model_id = model_id
        self.failed: str | None = None

    @property
    def ready(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        h = sum(ord(c) for c in text)
        return [float((h * 7) % 97) / 97.0, float((h * 13) % 89) / 89.0]


def _legacy_long_drawer(store: NativeMemoryStore, wing: str, room: str, content: str):
    """A drawer scoped exactly like a pre-T6.2 `file()` call: content +
    scope edges only, no passages (simulates content filed before this
    feature landed)."""
    s = store.store
    ref = s.write_cell(content.encode("utf-8"))
    s.scope(ref, s.write_cell(f"wing:{wing}".encode()))
    s.scope(ref, s.write_cell(f"room:{room}".encode()))
    return ref


def _long_text(marker: str) -> str:
    lines = [f"user: filler turn {i} about the deploy pipeline" for i in range(60)]
    lines.append(f"assistant: {marker}")
    return "\n".join(lines)


class TestPassageBackfillSweep:
    def test_backfill_creates_passages_for_preexisting_long_drawers(self) -> None:
        store = NativeMemoryStore(record_access=False)
        content = _long_text("backfill-target")
        ref = _legacy_long_drawer(store, "w", "r", content)
        assert store.has_passages(ref) is False

        result = _sweep_passage_backfill(store, _FakeEmbedder(), threading.Lock())

        assert result["created"] == 1
        assert result["failed"] == 0
        assert store.has_passages(ref) is True

    def test_backfill_skips_drawers_that_already_have_passages(self) -> None:
        store = NativeMemoryStore(record_access=False)
        embedder = _FakeEmbedder()
        content = _long_text("already-done")
        # Filed through file() -- passages already exist.
        ref = store.file(wing="w", room="r", content=content, embedder=embedder)
        assert store.has_passages(ref) is True

        result = _sweep_passage_backfill(store, embedder, threading.Lock())
        assert result["created"] == 0

    def test_backfill_skips_short_drawers(self) -> None:
        store = NativeMemoryStore(record_access=False)
        _legacy_long_drawer(store, "w", "r", "a short pre-existing note")

        result = _sweep_passage_backfill(store, _FakeEmbedder(), threading.Lock())
        assert result["created"] == 0
