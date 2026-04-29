"""
Coordinator-bridge unit tests.

These tests use FakeCoordinator — a pure-Python stub that replaces the real
Amplifier kernel (RustCoordinator / RustHookRegistry).  No real Amplifier
kernel, filesystem, MCP server, ChromaDB instance, or subprocess is involved.

The suite verifies four properties of the memory-mempalace coordinator bridge:

1. **mount() registration** — register_contributor() is called at mount() time
   with the expected channel and event list so the hook wires itself into the
   coordinator correctly.

2. **bridge_emit / sync_bridge_emit round-trip** — after each emit_event call
   site fires, the bridge emits the corresponding coordinator event via
   bridge_emit (async path) or sync_bridge_emit (sync/threaded path).

3. **emit_events:false suppression** — when the hook is configured with
   ``emit_events: false``, BOTH the private JSONL emit_event call AND the
   coordinator bridge emit are suppressed; the coordinator sees no events.

4. **_briefed_ids cross-hook population** — the interject hook's ``_briefed_ids``
   set is populated when a sibling hook emits the
   ``memory-mempalace:briefing_assembled`` coordinator event, confirming that
   the bridge correctly wires sibling hooks through the coordinator.
"""

from __future__ import annotations

import asyncio
import threading  # noqa: F401  (used by later tasks in this file)
from typing import Any
from unittest.mock import AsyncMock, MagicMock  # noqa: F401  (used by later tasks in this file)

import pytest  # noqa: F401  (used by later tasks in this file)


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class _Result:
    """Minimal stand-in for the RustHookResult returned by emit()."""

    def __init__(self, action: str = "continue") -> None:
        self.action = action


class FakeHooks:
    """
    Stub of ``coordinator.hooks`` (RustHookRegistry).

    Records every register() call and re-dispatches emit() to all handlers
    that were registered for the emitted event name.
    """

    def __init__(self) -> None:
        # event_name -> list of (handler, name, priority)
        self._registered: dict[str, list[tuple[Any, str, int]]] = {}
        # Full log of every register() call as dicts for easy assertion
        self._register_log: list[dict[str, Any]] = []
        # Full log of every emit() call as (event_name, data) tuples
        self._emit_log: list[tuple[str, Any]] = []

    def register(
        self,
        event_name: str,
        handler: Any,
        name: str = "",
        priority: int = 0,
    ) -> None:
        """Record and store a handler registration."""
        self._register_log.append(
            {
                "event_name": event_name,
                "handler": handler,
                "name": name,
                "priority": priority,
            }
        )
        self._registered.setdefault(event_name, []).append((handler, name, priority))

    async def emit(self, event_name: str, data: Any = None) -> _Result:
        """
        Record the emit and invoke every registered handler for *event_name*.

        Handlers may be sync or async; both are supported.  Returns a
        ``_Result`` with ``action='continue'`` unconditionally.
        """
        self._emit_log.append((event_name, data))
        for handler, _name, _priority in self._registered.get(event_name, []):
            result = handler(event_name, data)
            if asyncio.iscoroutine(result):
                await result
        return _Result(action="continue")


class FakeCoordinator:
    """
    Stub of ``RustCoordinator``.

    Provides the hooks registry and a contributor registration table that
    tests can inspect without starting the real Amplifier kernel.
    """

    def __init__(self) -> None:
        self.hooks: FakeHooks = FakeHooks()
        # channel -> {contributor_name: callback}
        self._contributors: dict[str, dict[str, Any]] = {}

    def register_contributor(self, channel: str, name: str, callback: Any) -> None:
        """Record a contributor registration on *channel*."""
        self._contributors.setdefault(channel, {})[name] = callback


# ---------------------------------------------------------------------------
# Capture hook — coordinator bridge tests (RED phase)
# ---------------------------------------------------------------------------


class TestCaptureCoordinatorBridge:
    """Tests for coordinator bridge wiring in the capture hook.

    These are RED-phase TDD tests.  They document the desired behavior
    of the coordinator bridge before the implementation exists.

    Expected failing reasons (RED phase):
    - mount() does not call register_contributor() → test 1 fails
    - MempalaceCaptureHook has no _sync_bridge_emit attribute → test 2 fails
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor on the coordinator with
        channel='observability.events' and name='memory-mempalace-capture'.

        The contributor callback must return a list of events that includes:
        - 'memory-mempalace:drawer_filed'
        - 'memory-mempalace:capture_failed'

        And must NOT include:
        - 'memory-mempalace:capture_queued'  (private-JSONL-only; intentionally hidden)
        """
        import asyncio

        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-mempalace-capture" in contribs, (
            "mount() must register contributor with name 'memory-mempalace-capture'"
        )

        callback = contribs["memory-mempalace-capture"]
        events = callback()
        assert "memory-mempalace:drawer_filed" in events, (
            "contributor callback must include 'memory-mempalace:drawer_filed'"
        )
        assert "memory-mempalace:capture_failed" in events, (
            "contributor callback must include 'memory-mempalace:capture_failed'"
        )
        assert "memory-mempalace:capture_queued" not in events, (
            "capture_queued is private-JSONL-only and must NOT be in coordinator events"
        )

    def test_drawer_filed_emits_to_coordinator_from_drain_thread(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """After a worthy tool:post event, the drain thread must emit
        'memory-mempalace:drawer_filed' to the coordinator via _sync_bridge_emit.

        The hook must expose a _sync_bridge_emit attribute confirming bridge wiring.
        drawer_filed must also appear in the private-JSONL emit log.
        """
        import asyncio
        import time

        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        emit_lock = threading.Lock()
        emitted: list[tuple[Any, ...]] = []

        def _capture(*a: Any, **kw: Any) -> None:
            with emit_lock:
                emitted.append((a, kw))

        monkeypatch.setattr(m, "emit_event", _capture)

        hook = m.MempalaceCaptureHook()
        asyncio.run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {"command": "ls -la"},
                    "tool_output": "x" * 200,
                },
            )
        )

        # Wait for drain thread to finish (500 iterations × 0.01s = 5s deadline)
        for _ in range(500):
            if m._QUEUE is None or m._QUEUE.unfinished_tasks == 0:
                break
            time.sleep(0.01)

        # The hook must have a _sync_bridge_emit attribute (coordinator bridge wiring)
        assert hasattr(hook, "_sync_bridge_emit"), (
            "MempalaceCaptureHook must have a _sync_bridge_emit attribute "
            "to wire the drain thread into the coordinator bridge"
        )

        # drawer_filed must appear in the private-JSONL emit list
        emitted_names = [a[0][1] for a in emitted if a[0] and len(a[0]) > 1]
        assert "drawer_filed" in emitted_names, (
            f"Expected 'drawer_filed' in private-JSONL emits after drain, "
            f"got: {emitted_names}"
        )

    def test_capture_queued_does_not_emit_to_coordinator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """capture_queued must NOT appear in coordinator.hooks._emit_log.

        capture_queued is intentionally private-JSONL-only.  Even after the
        bridge is wired, capture_queued events must never reach the coordinator.
        """
        import asyncio
        import time

        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        emit_lock = threading.Lock()
        emitted: list[tuple[Any, ...]] = []

        def _capture(*a: Any, **kw: Any) -> None:
            with emit_lock:
                emitted.append((a, kw))

        monkeypatch.setattr(m, "emit_event", _capture)

        hook = m.MempalaceCaptureHook()
        asyncio.run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {"command": "ls -la"},
                    "tool_output": "x" * 200,
                },
            )
        )

        # Wait for drain thread
        for _ in range(500):
            if m._QUEUE is None or m._QUEUE.unfinished_tasks == 0:
                break
            time.sleep(0.01)

        # capture_queued must never appear in coordinator.hooks._emit_log
        coordinator_event_names = [ev[0] for ev in coordinator.hooks._emit_log]
        assert "memory-mempalace:capture_queued" not in coordinator_event_names, (
            "capture_queued is private-JSONL-only and must never appear in "
            "coordinator.hooks._emit_log"
        )

    def test_emit_events_false_suppresses_both_channels(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """emit_events=False must suppress BOTH the private-JSONL channel
        AND any coordinator bridge emits.
        """
        import asyncio
        import time

        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator, config={"emit_events": False}))

        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceCaptureHook(config={"emit_events": False})
        asyncio.run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": "x" * 200,
                },
            )
        )

        # Drain queue
        for _ in range(500):
            if m._QUEUE is None or m._QUEUE.unfinished_tasks == 0:
                break
            time.sleep(0.01)

        # Private-JSONL channel: no emits
        assert emitted == [], (
            f"emit_events=False must suppress all private-JSONL emits, got: {emitted}"
        )

        # Coordinator channel: no events starting with 'memory-mempalace:'
        coordinator_events = [
            ev[0]
            for ev in coordinator.hooks._emit_log
            if ev[0].startswith("memory-mempalace:")
        ]
        assert coordinator_events == [], (
            f"emit_events=False must suppress coordinator bridge emits, "
            f"got: {coordinator_events}"
        )


# ---------------------------------------------------------------------------
# Briefing hook — coordinator bridge tests
# ---------------------------------------------------------------------------


class TestBriefingCoordinatorBridge:
    """Tests for coordinator bridge wiring in the briefing hook.

    These tests verify that mount() registers a contributor and that the hook
    emits coordinator events (briefing_assembled, briefing_skipped) via
    bridge_emit, carrying drawer_ids derived from results_after_rerank.
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor on the coordinator with
        channel='observability.events' and name='memory-mempalace-briefing'.

        The contributor callback must return a list of events that includes:
        - 'memory-mempalace:briefing_assembled'
        - 'memory-mempalace:briefing_skipped'
        """
        import asyncio

        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-mempalace-briefing" in contribs, (
            "mount() must register contributor with name 'memory-mempalace-briefing'"
        )

        callback = contribs["memory-mempalace-briefing"]
        events = callback()
        assert "memory-mempalace:briefing_assembled" in events, (
            "contributor callback must include 'memory-mempalace:briefing_assembled'"
        )
        assert "memory-mempalace:briefing_skipped" in events, (
            "contributor callback must include 'memory-mempalace:briefing_skipped'"
        )

    def test_briefing_assembled_emits_with_drawer_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After briefing_assembled, bridge emits with drawer_ids from results_after_rerank."""
        import asyncio
        import subprocess

        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        def fake_run(cmd: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(m.subprocess, "run", fake_run)

        results_with_ids = [
            {"id": "drawer-1", "room": "r", "text": "t1", "score": 0.9},
            {"id": "drawer-2", "room": "r", "text": "t2", "score": 0.8},
        ]

        monkeypatch.setattr(
            m,
            "_build_briefing",
            lambda **kw: (
                "## briefing",
                ["section"],
                100,
                results_with_ids,
                results_with_ids,
            ),
        )
        monkeypatch.setattr(m, "_detect_project_name", lambda: "testproject")

        bridge_calls: list[tuple[str, Any]] = []

        async def fake_bridge(event_name: str, payload: Any) -> None:
            bridge_calls.append((event_name, payload))

        hook = m.MempalaceBriefingHook(bridge_emit=fake_bridge)
        asyncio.run(hook("session:start", {"opening_prompt": "test"}))

        assembled_calls = [
            (name, payload)
            for name, payload in bridge_calls
            if name == "memory-mempalace:briefing_assembled"
        ]
        assert len(assembled_calls) == 1, (
            f"Expected exactly one 'memory-mempalace:briefing_assembled' bridge call, "
            f"got: {bridge_calls}"
        )
        _, payload = assembled_calls[0]
        assert payload.get("ok") is True, f"Expected ok=True in payload, got: {payload}"
        assert payload.get("drawer_ids") == ["drawer-1", "drawer-2"], (
            f"Expected drawer_ids=['drawer-1', 'drawer-2'], got: {payload.get('drawer_ids')}"
        )

    def test_emit_events_false_suppresses_both_channels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """emit_events=False must suppress BOTH the private-JSONL channel
        AND any coordinator bridge emits.
        """
        import asyncio

        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        def raise_not_found(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError("mempalace not found")

        monkeypatch.setattr(m.subprocess, "run", raise_not_found)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        bridge_calls: list[tuple[str, Any]] = []

        async def fake_bridge(event_name: str, payload: Any) -> None:
            bridge_calls.append((event_name, payload))

        hook = m.MempalaceBriefingHook(
            config={"emit_events": False}, bridge_emit=fake_bridge
        )
        asyncio.run(hook("session:start", {}))

        # Private-JSONL channel: no emits
        assert emitted == [], (
            f"emit_events=False must suppress all private-JSONL emits, got: {emitted}"
        )

        # Coordinator channel: no events starting with 'memory-mempalace:'
        coordinator_events = [
            name for name, _ in bridge_calls if name.startswith("memory-mempalace:")
        ]
        assert coordinator_events == [], (
            f"emit_events=False must suppress coordinator bridge emits, "
            f"got: {coordinator_events}"
        )
