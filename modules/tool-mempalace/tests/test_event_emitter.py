"""
Tests for amplifier_module_tool_mempalace.event_emitter

Section 8.1 of spec-v1.2.0-gene-transfer.md
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import amplifier_module_tool_mempalace.event_emitter as ee
from amplifier_module_tool_mempalace.event_emitter import (
    emit_event,
    read_events,
    truncate_preview,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_emitter_state(monkeypatch):
    """Reset module-level cache between tests so they don't bleed state."""
    monkeypatch.setattr(ee, "_cached_session_id", None)
    yield
    monkeypatch.setattr(ee, "_cached_session_id", None)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Patch _mempalace_base to return tmp_path/.mempalace (which exists)."""
    mp = tmp_path / ".mempalace"
    mp.mkdir()
    monkeypatch.setattr(ee, "_mempalace_base", lambda: mp)
    return mp


# ---------------------------------------------------------------------------
# truncate_preview
# ---------------------------------------------------------------------------


class TestTruncatePreview:
    def test_preview_null(self):
        assert truncate_preview(None) is None

    def test_preview_truncation_short(self):
        text = "a" * 100
        assert truncate_preview(text) == text

    def test_preview_truncation_short_exact_boundary(self):
        text = "x" * 50
        assert truncate_preview(text) == text

    def test_preview_truncation_long(self):
        text = "b" * 200
        result = truncate_preview(text)
        assert result is not None
        assert result == "b" * 97 + "..."
        assert len(result) == 100

    def test_preview_truncation_newline(self):
        # Newline at position 50 — truncate there
        text = "x" * 50 + "\n" + "y" * 100
        result = truncate_preview(text)
        assert result == "x" * 50 + "..."

    def test_preview_truncation_newline_before_100(self):
        # Newline at position 10 — truncate there even though text is >100
        text = "hello\nworld" + "z" * 200
        result = truncate_preview(text)
        assert result == "hello..."

    def test_preview_truncation_newline_at_99(self):
        # Newline exactly at position 99 — still truncates at newline
        text = "a" * 99 + "\n" + "b" * 50
        result = truncate_preview(text)
        assert result == "a" * 99 + "..."

    def test_preview_no_newline_exactly_100(self):
        # Exactly 100 chars, no newline — use as-is
        text = "c" * 100
        assert truncate_preview(text) == text

    def test_preview_no_newline_101(self):
        # 101 chars, no newline → 97 + "..."
        text = "d" * 101
        assert truncate_preview(text) == "d" * 97 + "..."

    def test_preview_binary_bytes(self):
        assert truncate_preview(b"hello world") == "[binary, 11 bytes]"  # type: ignore[arg-type]

    def test_preview_binary_bytearray(self):
        assert truncate_preview(bytearray(b"data")) == "[binary, 4 bytes]"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# emit_event — basic correctness
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_emit_creates_directory_and_file(self, fake_home):
        sid = "test_session_create"
        emit_event(
            "mempalace-capture",
            "drawer_filed",
            session_id=sid,
            ok=True,
            data={"wing": "w"},
        )

        events_dir = fake_home / "events"
        assert events_dir.is_dir(), "events/ directory was not created"

        jsonl_file = events_dir / f"{sid}.jsonl"
        assert jsonl_file.exists(), "session JSONL file was not created"

    def test_emit_appends_valid_jsonl(self, fake_home):
        sid = "test_session_jsonl"
        for i in range(3):
            emit_event(
                "mempalace-briefing",
                "briefing_assembled",
                session_id=sid,
                ok=True,
                data={"project": f"proj_{i}"},
            )

        lines = (fake_home / "events" / f"{sid}.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3

        for line in lines:
            record = json.loads(line)
            # All required fields present
            assert "v" in record
            assert "ts" in record
            assert "sid" in record
            assert "hook" in record
            assert "event" in record
            assert "ok" in record
            assert "preview" in record  # can be null
            assert "data" in record
            # Correct values
            assert record["hook"] == "mempalace-briefing"
            assert record["event"] == "briefing_assembled"
            assert record["sid"] == sid
            assert record["ok"] is True

    def test_emit_schema_version(self, fake_home):
        sid = "test_schema_version"
        emit_event(
            "project-context",
            "coordination_read",
            session_id=sid,
            data={"files_read": []},
        )

        lines = (fake_home / "events" / f"{sid}.jsonl").read_text().strip().splitlines()
        for line in lines:
            record = json.loads(line)
            assert record["v"] == 1

    def test_emit_preview_stored(self, fake_home):
        sid = "test_preview_stored"
        emit_event(
            "mempalace-capture",
            "drawer_filed",
            session_id=sid,
            preview="short preview",
            data={},
        )
        line = (fake_home / "events" / f"{sid}.jsonl").read_text().strip()
        record = json.loads(line)
        assert record["preview"] == "short preview"

    def test_emit_preview_null(self, fake_home):
        sid = "test_preview_null"
        emit_event("mempalace-briefing", "briefing_assembled", session_id=sid, data={})
        line = (fake_home / "events" / f"{sid}.jsonl").read_text().strip()
        record = json.loads(line)
        assert record["preview"] is None

    def test_emit_data_never_null(self, fake_home):
        sid = "test_data_notnull"
        emit_event("mempalace-capture", "drawer_filed", session_id=sid)
        line = (fake_home / "events" / f"{sid}.jsonl").read_text().strip()
        record = json.loads(line)
        assert isinstance(record["data"], dict)

    def test_emit_ts_is_iso_utc(self, fake_home):
        sid = "test_ts_iso"
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})
        line = (fake_home / "events" / f"{sid}.jsonl").read_text().strip()
        record = json.loads(line)
        ts = record["ts"]
        # Should end with +00:00 (UTC) and be parseable
        assert "T" in ts
        assert "+00:00" in ts or ts.endswith("Z")

    def test_emit_concurrent_safety(self, fake_home):
        sid = "test_concurrent"
        n_emits = 100
        n_threads = 10

        def do_emit(i: int) -> None:
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                session_id=sid,
                ok=True,
                data={"idx": i},
            )

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            list(pool.map(do_emit, range(n_emits)))

        jsonl_file = fake_home / "events" / f"{sid}.jsonl"
        lines = jsonl_file.read_text().strip().splitlines()
        assert len(lines) == n_emits, f"Expected {n_emits} lines, got {len(lines)}"

        # Each line must be valid JSON
        for line in lines:
            record = json.loads(line)
            assert record["v"] == 1

    def test_emit_never_raises(self, tmp_path, monkeypatch):
        """Emit with a read-only events dir → no exception, silent return."""
        mp = tmp_path / ".mempalace"
        mp.mkdir()
        events_dir = mp / "events"
        events_dir.mkdir()
        events_dir.chmod(0o555)  # read + execute, no write

        monkeypatch.setattr(ee, "_mempalace_base", lambda: mp)

        try:
            # Must not raise
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                session_id="readonly_test",
                data={"wing": "w"},
            )
        finally:
            events_dir.chmod(0o755)  # restore so tmp_path cleanup works

    def test_emit_no_mempalace_dir(self, tmp_path, monkeypatch):
        """If ~/.mempalace/ doesn't exist → silent no-op, no file created."""
        # Patch _mempalace_base to return None (parent dir missing)
        monkeypatch.setattr(ee, "_mempalace_base", lambda: None)

        emit_event(
            "mempalace-capture",
            "drawer_filed",
            session_id="no_mempalace",
            data={"wing": "w"},
        )
        # Nothing should be created
        assert not (tmp_path / ".mempalace").exists()

    def test_emit_appends_multiple_sessions(self, fake_home):
        """Events for different sessions go to different files."""
        emit_event("mempalace-capture", "drawer_filed", session_id="sess_a", data={})
        emit_event("mempalace-capture", "drawer_filed", session_id="sess_b", data={})
        emit_event("mempalace-capture", "drawer_filed", session_id="sess_a", data={})

        file_a = fake_home / "events" / "sess_a.jsonl"
        file_b = fake_home / "events" / "sess_b.jsonl"
        assert len(file_a.read_text().strip().splitlines()) == 2
        assert len(file_b.read_text().strip().splitlines()) == 1


# ---------------------------------------------------------------------------
# Session ID fallback
# ---------------------------------------------------------------------------


class TestSessionIdFallback:
    def test_session_id_fallback(self, fake_home, monkeypatch):
        """No env var, no explicit ID → falls back to pid_* format."""
        monkeypatch.delenv("AMPLIFIER_SESSION_ID", raising=False)
        monkeypatch.setattr(ee, "_cached_session_id", None)

        emit_event("mempalace-capture", "drawer_filed", data={})

        events_dir = fake_home / "events"
        files = list(events_dir.glob("pid_*.jsonl"))
        assert len(files) == 1, f"Expected one pid_*.jsonl file, got: {files}"
        name = files[0].stem
        assert name.startswith("pid_"), f"Expected pid_ prefix, got {name!r}"
        # Format: pid_{pid}_{YYYY-MM-DD}
        parts = name.split("_")
        assert len(parts) == 3
        assert parts[1].isdigit()

    def test_session_id_env_var(self, fake_home, monkeypatch):
        """AMPLIFIER_SESSION_ID env var is used when set."""
        monkeypatch.setenv("AMPLIFIER_SESSION_ID", "env_session_xyz")
        monkeypatch.setattr(ee, "_cached_session_id", None)

        emit_event("mempalace-capture", "drawer_filed", data={})

        assert (fake_home / "events" / "env_session_xyz.jsonl").exists()

    def test_explicit_session_id_overrides_env(self, fake_home, monkeypatch):
        """Explicit session_id takes priority over env var."""
        monkeypatch.setenv("AMPLIFIER_SESSION_ID", "env_session_xyz")
        monkeypatch.setattr(ee, "_cached_session_id", None)

        emit_event(
            "mempalace-capture", "drawer_filed", session_id="explicit_id", data={}
        )

        assert (fake_home / "events" / "explicit_id.jsonl").exists()
        assert not (fake_home / "events" / "env_session_xyz.jsonl").exists()


# ---------------------------------------------------------------------------
# read_events
# ---------------------------------------------------------------------------


class TestReadEvents:
    def test_read_events_basic(self, fake_home):
        sid = "read_basic"
        for i in range(5):
            emit_event(
                "mempalace-capture", "drawer_filed", session_id=sid, data={"n": i}
            )

        events = read_events(session_id=sid)
        assert len(events) == 5
        for ev in events:
            assert ev["hook"] == "mempalace-capture"
            assert ev["event"] == "drawer_filed"

    def test_read_events_tail(self, fake_home):
        sid = "read_tail"
        for i in range(10):
            emit_event(
                "mempalace-capture", "drawer_filed", session_id=sid, data={"n": i}
            )

        events = read_events(session_id=sid, tail=True, limit=3)
        assert len(events) == 3
        # Should be the last 3 (indices 7, 8, 9)
        ns = [e["data"]["n"] for e in events]
        assert ns == [7, 8, 9]

    def test_read_events_head(self, fake_home):
        sid = "read_head"
        for i in range(10):
            emit_event(
                "mempalace-capture", "drawer_filed", session_id=sid, data={"n": i}
            )

        events = read_events(session_id=sid, tail=False, limit=3)
        assert len(events) == 3
        ns = [e["data"]["n"] for e in events]
        assert ns == [0, 1, 2]

    def test_read_events_filter_hook(self, fake_home):
        sid = "read_filter"
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})
        emit_event("mempalace-briefing", "briefing_assembled", session_id=sid, data={})
        emit_event(
            "mempalace-capture",
            "capture_skipped",
            session_id=sid,
            data={"reason": "too_short"},
        )

        events = read_events(session_id=sid, hook_filter="mempalace-capture")
        assert len(events) == 2
        assert all(e["hook"] == "mempalace-capture" for e in events)

    def test_read_events_filter_event(self, fake_home):
        sid = "read_filter_event"
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})
        emit_event(
            "mempalace-capture",
            "capture_skipped",
            session_id=sid,
            data={"reason": "too_short"},
        )
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})

        events = read_events(session_id=sid, event_filter="drawer_filed")
        assert len(events) == 2
        assert all(e["event"] == "drawer_filed" for e in events)

    def test_read_events_missing_file(self, fake_home):
        events = read_events(session_id="nonexistent_session_xyz_abc")
        assert events == []

    def test_read_events_missing_file_no_base(self, monkeypatch):
        """If _mempalace_base returns None, read_events returns empty list."""
        monkeypatch.setattr(ee, "_mempalace_base", lambda: None)
        events = read_events(session_id="any_session")
        assert events == []
