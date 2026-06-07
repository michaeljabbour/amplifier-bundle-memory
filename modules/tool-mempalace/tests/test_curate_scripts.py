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
    DualWriteMemoryStore,
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

    def test_amplifierdata_store_files_when_available(self) -> None:
        # The amplifier-data seam is now WIRED (full coverage in
        # test_amplifier_data_store.py). Confirm it is no longer a stub: with
        # the optional dependency present it stores and returns a content ref.
        pytest.importorskip("amplifier_data")
        store = AmplifierDataMemoryStore(record_access=False)
        store.file(wing="w", room="r", content="c", category="decision")
        ref = store.filed[-1]["ref"]
        assert isinstance(ref, str) and ref


class TestDualWriteMemoryStore:
    def test_writes_to_both_stores(self) -> None:
        primary = RecordingMemoryStore()
        shadow = RecordingMemoryStore()
        dw = DualWriteMemoryStore(primary, shadow)
        write_cells(
            [{"wing": "w", "room": "r", "content": "x", "category": "decision"}], dw
        )
        assert len(primary.filed) == 1
        assert len(shadow.filed) == 1
        assert primary.filed[0]["content"] == shadow.filed[0]["content"] == "x"

    def test_shadow_failure_does_not_break_primary(self) -> None:
        class _Boom:
            def file(self, **_: object) -> None:
                raise RuntimeError("shadow down")

        primary = RecordingMemoryStore()
        dw = DualWriteMemoryStore(primary, _Boom())
        n = write_cells([{"wing": "w", "room": "r", "content": "x"}], dw)
        assert n == 1
        assert len(primary.filed) == 1  # primary unaffected
        assert dw.shadow_errors and "shadow down" in dw.shadow_errors[0]

    def test_fail_on_shadow_error_raises(self) -> None:
        class _Boom:
            def file(self, **_: object) -> None:
                raise RuntimeError("shadow down")

        dw = DualWriteMemoryStore(
            RecordingMemoryStore(), _Boom(), fail_on_shadow_error=True
        )
        with pytest.raises(RuntimeError):
            dw.file(wing="w", room="r", content="x")
