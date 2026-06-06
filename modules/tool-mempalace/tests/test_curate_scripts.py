"""
Tests for the Phase 2 consolidation-pipeline scripts:

    scripts.load_captures  — read a session's filed drawers from the event log
    scripts.write_cells    — file consolidated cells through a MemoryStore
    scripts.memory_store   — the storage seam (palace today, amplifier-data later)

Pure unit tests: no mempalace CLI, no attractor engine, no network. The scripts
are designed so the data path is fully testable without either dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_module_tool_mempalace.scripts.load_captures import load_captures
from amplifier_module_tool_mempalace.scripts.memory_store import (
    AmplifierDataMemoryStore,
    RecordingMemoryStore,
)
from amplifier_module_tool_mempalace.scripts.write_cells import write_cells


def _write_events(events_root: Path, sid: str, lines: list[dict]) -> None:
    events_root.mkdir(parents=True, exist_ok=True)
    f = events_root / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _filed(capture_id: str, room: str, category: str | None) -> dict:
    return {
        "v": 1,
        "ts": "2026-06-05T00:00:00Z",
        "sid": "s1",
        "hook": "mempalace-capture",
        "event": "drawer_filed",
        "ok": True,
        "preview": f"preview of {capture_id}",
        "data": {
            "capture_id": capture_id,
            "wing": "wing_demo",
            "room": room,
            "category": category,
            "content_bytes": 123,
            "source": "bash",
        },
    }


# ---------------------------------------------------------------------------
# load_captures
# ---------------------------------------------------------------------------


class TestLoadCaptures:
    def test_reads_filed_drawers(self, tmp_path: Path) -> None:
        _write_events(
            tmp_path,
            "s1",
            [
                _filed("a", "shell-commands-decision", "decision"),
                _filed("b", "file-reads", None),
            ],
        )
        rows = load_captures("s1", events_root=tmp_path)
        assert len(rows) == 2
        assert {r["capture_id"] for r in rows} == {"a", "b"}
        first = next(r for r in rows if r["capture_id"] == "a")
        assert first["room"] == "shell-commands-decision"
        assert first["category"] == "decision"
        assert first["preview"] == "preview of a"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_captures("nope", events_root=tmp_path) == []

    def test_ignores_non_filed_events(self, tmp_path: Path) -> None:
        _write_events(
            tmp_path,
            "s1",
            [
                {"event": "capture_skipped", "data": {"reason": "too_short"}},
                {"event": "capture_queued", "data": {"capture_id": "x"}},
                _filed("keep", "r", "pattern"),
            ],
        )
        rows = load_captures("s1", events_root=tmp_path)
        assert [r["capture_id"] for r in rows] == ["keep"]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        events_root = tmp_path
        events_root.mkdir(parents=True, exist_ok=True)
        f = events_root / "s1.jsonl"
        f.write_text(
            json.dumps(_filed("good", "r", None)) + "\n{ this is not json\n",
            encoding="utf-8",
        )
        rows = load_captures("s1", events_root=events_root)
        assert [r["capture_id"] for r in rows] == ["good"]


# ---------------------------------------------------------------------------
# write_cells
# ---------------------------------------------------------------------------


class TestWriteCells:
    def test_files_each_cell(self) -> None:
        store = RecordingMemoryStore()
        cells = [
            {"wing": "w", "room": "r1", "content": "alpha", "category": "decision"},
            {"wing": "w", "room": "r2", "content": "beta", "importance": 0.6},
        ]
        n = write_cells(cells, store)
        assert n == 2
        assert len(store.filed) == 2
        assert store.filed[0]["content"] == "alpha"
        assert store.filed[0]["category"] == "decision"
        assert store.filed[1]["importance"] == 0.6

    def test_empty_cells(self) -> None:
        store = RecordingMemoryStore()
        assert write_cells([], store) == 0
        assert store.filed == []

    def test_defaults_for_optional_fields(self) -> None:
        store = RecordingMemoryStore()
        write_cells([{"wing": "w", "room": "r", "content": "x"}], store)
        rec = store.filed[0]
        assert rec["source"] == ""
        assert rec["category"] is None
        assert rec["importance"] is None


# ---------------------------------------------------------------------------
# memory_store seam
# ---------------------------------------------------------------------------


class TestMemoryStoreSeam:
    def test_recording_store_records_fields(self) -> None:
        store = RecordingMemoryStore()
        store.file(
            wing="w",
            room="r",
            content="c",
            source="s",
            category="pattern",
            importance=0.5,
        )
        assert store.filed == [
            {
                "wing": "w",
                "room": "r",
                "content": "c",
                "source": "s",
                "category": "pattern",
                "importance": 0.5,
            }
        ]

    def test_amplifierdata_store_is_a_documented_stub(self) -> None:
        # Phase 3 seam: the amplifier-data substrate is not wired yet
        # (blocked on persistence + vector lens). It must fail loudly, not
        # silently pretend to store.
        store = AmplifierDataMemoryStore()
        with pytest.raises(NotImplementedError):
            store.file(wing="w", room="r", content="c")
