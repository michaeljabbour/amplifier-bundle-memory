"""
Tests for PalaceTool `events` operation.

Section 8.2 of spec-v1.2.0-gene-transfer.md
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import amplifier_module_tool_mempalace.event_emitter as ee
from amplifier_module_tool_mempalace import PalaceTool
from amplifier_module_tool_mempalace.event_emitter import emit_event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_emitter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset module-level session-id cache between tests."""
    monkeypatch.setattr(ee, "_cached_session_id", None)


@pytest.fixture()
def fake_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """Redirect emit/read I/O to a temp .mempalace directory."""
    mp = tmp_path / ".mempalace"
    mp.mkdir()
    monkeypatch.setattr(ee, "_mempalace_base", lambda: mp)
    return mp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously (helper for async execute calls)."""
    return asyncio.run(coro)


def _result_json(tool_result) -> dict:
    """Extract and parse JSON from a ToolResult — reads .output field."""
    return json.loads(tool_result.output)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventsOperationRegistered:
    def test_events_operation_registered(self) -> None:
        """'events' must appear in the operation enum in PalaceTool.parameters."""
        tool = PalaceTool()
        enum_values: list[str] = tool.parameters["properties"]["operation"]["enum"]
        assert "events" in enum_values, f"'events' not in operation enum: {enum_values}"


class TestEventsReturnsJson:
    def test_events_returns_json(self, fake_home: Any) -> None:
        """execute(operation='events') returns valid JSON with required shape."""
        sid = "test_returns_json"
        # Write 3 events into the fake home
        for i in range(3):
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                session_id=sid,
                data={"wing": "w", "n": i},
            )

        tool = PalaceTool()
        result = _run(tool.execute(operation="events", session_id=sid))

        payload = _result_json(result)

        # Required top-level keys
        assert "session_id" in payload
        assert "event_count" in payload
        assert "returned" in payload
        assert "events" in payload

        # Values make sense
        assert payload["session_id"] == sid
        assert payload["event_count"] == 3
        assert payload["returned"] == 3
        assert len(payload["events"]) == 3

        # Each event has the required schema fields
        for ev in payload["events"]:
            assert ev["v"] == 1
            assert ev["hook"] == "mempalace-capture"
            assert ev["event"] == "drawer_filed"

    def test_events_tail_mode_default(self, fake_home: Any) -> None:
        """Default tail=True with limit < total returns the LAST N events."""
        sid = "test_tail_default"
        for i in range(10):
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                session_id=sid,
                data={"n": i},
            )

        tool = PalaceTool()
        result = _run(tool.execute(operation="events", session_id=sid, limit=3))
        payload = _result_json(result)

        assert payload["returned"] == 3
        assert payload["event_count"] == 10
        # tail=True by default → last 3 (n=7, 8, 9)
        ns = [ev["data"]["n"] for ev in payload["events"]]
        assert ns == [7, 8, 9]

    def test_events_head_mode(self, fake_home: Any) -> None:
        """tail=False returns the FIRST N events."""
        sid = "test_head_mode"
        for i in range(10):
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                session_id=sid,
                data={"n": i},
            )

        tool = PalaceTool()
        result = _run(
            tool.execute(operation="events", session_id=sid, limit=3, tail=False)
        )
        payload = _result_json(result)

        assert payload["returned"] == 3
        assert payload["event_count"] == 10
        ns = [ev["data"]["n"] for ev in payload["events"]]
        assert ns == [0, 1, 2]

    def test_events_limit_capped_at_200(self, fake_home: Any) -> None:
        """limit > 200 is silently clamped to 200."""
        sid = "test_limit_cap"
        # Write 5 events — requesting limit=9999 but only 5 exist
        for i in range(5):
            emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})

        tool = PalaceTool()
        result = _run(tool.execute(operation="events", session_id=sid, limit=9999))
        payload = _result_json(result)

        # Should not error; returns what's available (≤ 200)
        assert payload["event_count"] == 5
        assert payload["returned"] == 5


class TestEventsWithFilters:
    def test_events_with_filters(self, fake_home: Any) -> None:
        """hook_filter and event_filter correctly narrow results."""
        sid = "test_filters"
        # Write a mix of events
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={})
        emit_event(
            "mempalace-capture",
            "capture_skipped",
            ok=False,
            session_id=sid,
            data={"reason": "too_short"},
        )
        emit_event("mempalace-briefing", "briefing_assembled", session_id=sid, data={})

        tool = PalaceTool()

        # Filter by hook only
        result = _run(
            tool.execute(
                operation="events",
                session_id=sid,
                hook_filter="mempalace-capture",
            )
        )
        payload = _result_json(result)
        assert payload["event_count"] == 3
        assert all(ev["hook"] == "mempalace-capture" for ev in payload["events"])

        # Filter by event_type only
        result2 = _run(
            tool.execute(
                operation="events",
                session_id=sid,
                event_filter="drawer_filed",
            )
        )
        payload2 = _result_json(result2)
        assert payload2["event_count"] == 2
        assert all(ev["event"] == "drawer_filed" for ev in payload2["events"])

        # Filter by both hook + event
        result3 = _run(
            tool.execute(
                operation="events",
                session_id=sid,
                hook_filter="mempalace-capture",
                event_filter="capture_skipped",
            )
        )
        payload3 = _result_json(result3)
        assert payload3["event_count"] == 1
        assert payload3["events"][0]["event"] == "capture_skipped"


class TestEventsEmptySession:
    def test_events_empty_session(self, fake_home: Any) -> None:
        """Unknown session_id → event_count: 0, returned: 0, events: [] — no error."""
        tool = PalaceTool()
        result = _run(
            tool.execute(operation="events", session_id="nonexistent_session_xyz_abc")
        )
        payload = _result_json(result)

        assert payload["session_id"] == "nonexistent_session_xyz_abc"
        assert payload["event_count"] == 0
        assert payload["returned"] == 0
        assert payload["events"] == []

    def test_events_no_mempalace_dir(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ~/.mempalace/ doesn't exist, return empty result — no error."""
        monkeypatch.setattr(ee, "_mempalace_base", lambda: None)

        tool = PalaceTool()
        result = _run(tool.execute(operation="events", session_id="some_session"))
        payload = _result_json(result)

        assert payload["event_count"] == 0
        assert payload["returned"] == 0
        assert payload["events"] == []

    def test_events_skipped_lines_on_corrupt_jsonl(self, fake_home: Any) -> None:
        """Corrupt JSONL lines → skipped_lines count in response; valid lines read normally."""
        sid = "test_corrupt_jsonl"
        # Write 2 valid events via emit
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={"n": 0})
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={"n": 1})

        # Inject a corrupt line directly into the file
        events_file = fake_home / "events" / f"{sid}.jsonl"
        with events_file.open("a") as fh:
            fh.write("this is not valid json\n")

        # Write 1 more valid event
        emit_event("mempalace-capture", "drawer_filed", session_id=sid, data={"n": 2})

        tool = PalaceTool()
        result = _run(tool.execute(operation="events", session_id=sid))
        payload = _result_json(result)

        assert payload["event_count"] == 3, f"Expected 3 valid events, got {payload}"
        assert payload["returned"] == 3
        assert payload["skipped_lines"] == 1
        assert len(payload["events"]) == 3
