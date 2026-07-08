"""Tests for the capture_skipped -> coordinator bridge (hardening change).

hooks-memory-capture writes a `capture_skipped` event into the memory-side
event log (emit_event -> ~/.amplifier/memory/events/{session}.jsonl)
whenever a tool:post payload fails a gate -- too_short/too_long/skip_tool
(the worthiness gate), or category_filtered (the categories allowlist).
Previously that event never reached the session's own events.jsonl (the
file a live DTU session writes via the coordinator, and the one
tests/integration/test_event_wiring.py and human debugging grep) because
only `drawer_filed`/`capture_failed` were bridged via `self._bridge_emit`
(-> coordinator.hooks.emit -> events.jsonl). These tests prove
capture_skipped now goes through the SAME bridge seam drawer_filed uses,
so a future "why didn't my drawer get filed" investigation is one grep
away instead of requiring access to the memory-side log.

The bridge call happens synchronously in the hot-path `__call__` handler
(the gates are decided before spooling to the drain thread), so it is
exercised directly here without needing the drain thread or a real
coordinator -- `bridge_emit` is a plain recording spy, matching the
`SyncBridge = Callable[[str, Any], None]` contract in coordinator_bridge.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_capture as capture_module
from amplifier_module_hooks_memory_capture import MemoryCaptureHook


def _run(coro):
    return asyncio.run(coro)


def _make_hook(monkeypatch, **config: Any) -> tuple[MemoryCaptureHook, list[tuple[str, Any]]]:
    """Build a hook with emit_event silenced (no real-home side effects) and
    a recording bridge_emit spy."""
    monkeypatch.setattr(capture_module, "emit_event", lambda *a, **k: None)
    bridged: list[tuple[str, Any]] = []

    def bridge_emit(event: str, payload: Any) -> None:
        bridged.append((event, payload))

    hook = MemoryCaptureHook(config=config, bridge_emit=bridge_emit)
    return hook, bridged


def test_capture_skipped_too_short_is_bridged(monkeypatch) -> None:
    hook, bridged = _make_hook(monkeypatch, emit_events=True)

    result = _run(
        hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "short",  # < 50 chars -> too_short
                "session_id": "test-session",
            },
        )
    )

    assert result.action == "continue"
    assert bridged, "capture_skipped (too_short) was not bridged to the coordinator"
    event, payload = bridged[-1]
    assert event == "memory:capture_skipped"
    assert payload["reason"] == "too_short"
    assert payload["ok"] is False
    assert payload["tool_name"] == "bash"


def test_capture_skipped_too_long_is_bridged(monkeypatch) -> None:
    hook, bridged = _make_hook(monkeypatch, emit_events=True)

    result = _run(
        hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "x" * 9000,  # > 8192 -> too_long
                "session_id": "test-session",
            },
        )
    )

    assert result.action == "continue"
    assert bridged, "capture_skipped (too_long) was not bridged to the coordinator"
    event, payload = bridged[-1]
    assert event == "memory:capture_skipped"
    assert payload["reason"] == "too_long"


def test_capture_skipped_category_filtered_is_bridged(monkeypatch) -> None:
    hook, bridged = _make_hook(monkeypatch, emit_events=True, categories=["decision"])

    # Long enough to pass the worthiness gate but contains no seed word from
    # any attractor, so category resolves to None -- not in the configured
    # categories allowlist.
    long_uncategorized = "x" * 60
    result = _run(
        hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": long_uncategorized,
                "session_id": "test-session",
            },
        )
    )

    assert result.action == "continue"
    assert bridged, "capture_skipped (category_filtered) was not bridged"
    event, payload = bridged[-1]
    assert event == "memory:capture_skipped"
    assert payload["reason"] == "category_filtered"
    assert payload["ok"] is False


def test_capture_skipped_not_bridged_when_emit_events_false(monkeypatch) -> None:
    hook, bridged = _make_hook(monkeypatch, emit_events=False)

    result = _run(
        hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "short",
                "session_id": "test-session",
            },
        )
    )

    assert result.action == "continue"
    assert not bridged, "capture_skipped must not bridge when emit_events=False"


def test_mount_registers_capture_skipped_event_name() -> None:
    """register_events() must declare memory:capture_skipped alongside the
    pre-existing drawer_filed/capture_failed names, or observability
    contributors that enumerate declared events won't know it exists."""
    import inspect

    src = inspect.getsource(capture_module.mount)
    assert "memory:capture_skipped" in src
    assert "memory:drawer_filed" in src
    assert "memory:capture_failed" in src
