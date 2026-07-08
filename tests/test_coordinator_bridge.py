"""
Coordinator-bridge unit tests.

These tests use FakeCoordinator — a pure-Python stub that replaces the real
Amplifier kernel (RustCoordinator / RustHookRegistry).  No real Amplifier
kernel, filesystem, MCP server, ChromaDB instance, or subprocess is involved.

The suite verifies four properties of the memory coordinator bridge:

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
   ``memory:briefing_assembled`` coordinator event, confirming that
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

    async def mount(self, channel: str, tool: Any, *, name: str = "") -> None:
        """No-op fake mount — records nothing, satisfies tool mount() contract."""


# ---------------------------------------------------------------------------
# Capture hook — coordinator bridge tests (RED phase)
# ---------------------------------------------------------------------------


class TestCaptureCoordinatorBridge:
    """Tests for coordinator bridge wiring in the capture hook.

    These are RED-phase TDD tests.  They document the desired behavior
    of the coordinator bridge before the implementation exists.

    Expected failing reasons (RED phase):
    - mount() does not call register_contributor() → test 1 fails
    - MemoryCaptureHook has no _sync_bridge_emit attribute → test 2 fails
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor on the coordinator with
        channel='observability.events' and name='memory-capture'.

        The contributor callback must return a list of events that includes:
        - 'memory:drawer_filed'
        - 'memory:capture_failed'

        And must NOT include:
        - 'memory:capture_queued'  (private-JSONL-only; intentionally hidden)
        """
        import asyncio

        import amplifier_module_hooks_memory_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-capture" in contribs, (
            "mount() must register contributor with name 'memory-capture'"
        )

        callback = contribs["memory-capture"]
        events = callback()
        assert "memory:drawer_filed" in events, (
            "contributor callback must include 'memory:drawer_filed'"
        )
        assert "memory:capture_failed" in events, (
            "contributor callback must include 'memory:capture_failed'"
        )
        assert "memory:capture_queued" not in events, (
            "capture_queued is private-JSONL-only and must NOT be in coordinator events"
        )

    def test_drawer_filed_emits_to_coordinator_from_drain_thread(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """After a worthy tool:post event, the drain thread must emit
        'memory:drawer_filed' to the coordinator via _bridge_emit.

        The hook must expose a _bridge_emit attribute confirming bridge wiring.
        drawer_filed must also appear in the private-JSONL emit log.
        """
        import asyncio
        import time

        import amplifier_module_hooks_memory_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))

        emit_lock = threading.Lock()
        emitted: list[tuple[Any, ...]] = []

        def _capture(*a: Any, **kw: Any) -> None:
            with emit_lock:
                emitted.append((a, kw))

        monkeypatch.setattr(m, "emit_event", _capture)

        hook = m.MemoryCaptureHook()
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

        # The hook must have a _bridge_emit attribute (coordinator bridge wiring)
        assert hasattr(hook, "_bridge_emit"), (
            "MemoryCaptureHook must have a _bridge_emit attribute "
            "to wire the drain thread into the coordinator bridge"
        )

        # drawer_filed must appear in the private-JSONL emit list
        emitted_names = [a[0][1] for a in emitted if a[0] and len(a[0]) > 1]
        assert "drawer_filed" in emitted_names, (
            f"Expected 'drawer_filed' in private-JSONL emits after drain, got: {emitted_names}"
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

        import amplifier_module_hooks_memory_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))

        emit_lock = threading.Lock()
        emitted: list[tuple[Any, ...]] = []

        def _capture(*a: Any, **kw: Any) -> None:
            with emit_lock:
                emitted.append((a, kw))

        monkeypatch.setattr(m, "emit_event", _capture)

        hook = m.MemoryCaptureHook()
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
        assert "memory:capture_queued" not in coordinator_event_names, (
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

        import amplifier_module_hooks_memory_capture as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator, config={"emit_events": False}))

        monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MemoryCaptureHook(config={"emit_events": False})
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

        # Coordinator channel: no events starting with 'memory:'
        coordinator_events = [
            ev[0] for ev in coordinator.hooks._emit_log if ev[0].startswith("memory:")
        ]
        assert coordinator_events == [], (
            f"emit_events=False must suppress coordinator bridge emits, got: {coordinator_events}"
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
        channel='observability.events' and name='memory-briefing'.

        The contributor callback must return a list of events that includes:
        - 'memory:briefing_assembled'
        - 'memory:briefing_skipped'
        """
        import asyncio

        import amplifier_module_hooks_memory_briefing as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-briefing" in contribs, (
            "mount() must register contributor with name 'memory-briefing'"
        )

        callback = contribs["memory-briefing"]
        events = callback()
        assert "memory:briefing_assembled" in events, (
            "contributor callback must include 'memory:briefing_assembled'"
        )
        assert "memory:briefing_skipped" in events, (
            "contributor callback must include 'memory:briefing_skipped'"
        )

    def test_briefing_assembled_emits_with_drawer_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After briefing_assembled, bridge emits with drawer_ids from results_after_rerank."""
        import asyncio

        import amplifier_module_hooks_memory_briefing as m  # type: ignore[import]

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

        hook = m.MemoryBriefingHook(bridge_emit=fake_bridge)
        asyncio.run(hook("session:start", {"opening_prompt": "test"}))

        assembled_calls = [
            (name, payload) for name, payload in bridge_calls if name == "memory:briefing_assembled"
        ]
        assert len(assembled_calls) == 1, (
            f"Expected exactly one 'memory:briefing_assembled' bridge call, got: {bridge_calls}"
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

        import amplifier_module_hooks_memory_briefing as m  # type: ignore[import]

        monkeypatch.setattr(m, "ensure_daemon", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        bridge_calls: list[tuple[str, Any]] = []

        async def fake_bridge(event_name: str, payload: Any) -> None:
            bridge_calls.append((event_name, payload))

        hook = m.MemoryBriefingHook(config={"emit_events": False}, bridge_emit=fake_bridge)
        asyncio.run(hook("session:start", {}))

        # Private-JSONL channel: no emits
        assert emitted == [], (
            f"emit_events=False must suppress all private-JSONL emits, got: {emitted}"
        )

        # Coordinator channel: no events starting with 'memory:'
        coordinator_events = [name for name, _ in bridge_calls if name.startswith("memory:")]
        assert coordinator_events == [], (
            f"emit_events=False must suppress coordinator bridge emits, got: {coordinator_events}"
        )


# ---------------------------------------------------------------------------
# Interject hook — coordinator bridge tests
# ---------------------------------------------------------------------------


class TestInterjectCoordinatorBridge:
    """Tests for coordinator bridge wiring in the interject hook.

    These tests verify that mount() registers a contributor, registers a
    cross-hook listener for briefing_assembled, and emits coordinator events
    (memory_surfaced, interject_skipped) via bridge_emit.
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor with contributor name
        'memory-interject'. The callback must return a list that
        includes 'memory:memory_surfaced' and
        'memory:interject_skipped'.
        """
        import asyncio

        import amplifier_module_hooks_memory_interject as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-interject" in contribs, (
            "mount() must register contributor with name 'memory-interject'"
        )

        callback = contribs["memory-interject"]
        events = callback()
        assert "memory:memory_surfaced" in events, (
            "contributor callback must include 'memory:memory_surfaced'"
        )
        assert "memory:interject_skipped" in events, (
            "contributor callback must include 'memory:interject_skipped'"
        )

    def test_briefing_assembled_listener_registered_in_mount(self) -> None:
        """mount() must register a handler for 'memory:briefing_assembled'
        in coordinator.hooks._registered so that when the briefing hook emits
        briefing_assembled, the interject hook's _briefed_ids is updated.
        """
        import asyncio

        import amplifier_module_hooks_memory_interject as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "memory:briefing_assembled" in coordinator.hooks._registered, (
            "mount() must register a handler for 'memory:briefing_assembled' "
            "so that briefing events update _briefed_ids"
        )

    async def test_briefed_ids_populated_from_briefing_event(self) -> None:
        """After mount(), emitting 'memory:briefing_assembled' with
        drawer_ids must populate the interject hook's _briefed_ids set.

        1. Find hook via prompt:submit registered handler (bound method).
        2. Assert _briefed_ids starts empty.
        3. Emit briefing_assembled with drawer_ids=['d-1', 'd-2'].
        4. Assert _briefed_ids == {'d-1', 'd-2'}.
        """
        import amplifier_module_hooks_memory_interject as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        await m.mount(coordinator)

        # Find hook via the prompt:submit registered handler (bound method)
        handlers = coordinator.hooks._registered.get("prompt:submit", [])
        assert handlers, "Expected prompt:submit handler registered after mount()"
        handler = handlers[0][0]  # (handler, name, priority)
        hook = handler.__self__

        # Initially _briefed_ids must be empty
        assert hook._briefed_ids == set(), (
            f"Expected _briefed_ids == set() before briefing event, got: {hook._briefed_ids}"
        )

        # Emit briefing_assembled — the registered listener must update _briefed_ids
        await coordinator.hooks.emit(
            "memory:briefing_assembled",
            {"drawer_ids": ["d-1", "d-2"]},
        )

        assert hook._briefed_ids == {"d-1", "d-2"}, (
            f"Expected _briefed_ids == {{'d-1', 'd-2'}} after briefing event, "
            f"got: {hook._briefed_ids}"
        )

    async def test_memory_surfaced_emits_to_coordinator(self) -> None:
        """After mount(), calling on_prompt_submit with a matching memory must
        emit exactly one 'memory:memory_surfaced' event to the
        coordinator with ok=True, trigger='prompt_submit', memory_ids=['m1'].
        """
        import amplifier_module_hooks_memory_interject as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        await m.mount(coordinator)

        # Find hook via prompt:submit registered handler
        handlers = coordinator.hooks._registered.get("prompt:submit", [])
        assert handlers, "Expected prompt:submit handler registered after mount()"
        handler = handlers[0][0]  # (handler, name, priority)
        hook = handler.__self__

        # Stub _retrieve_and_gate to return one matching memory
        async def _fake_retrieve(query: str, event: str):  # type: ignore[no-untyped-def]
            return ([{"id": "m1", "text": "hello", "score": 0.9}], True, "", False)

        hook._retrieve_and_gate = _fake_retrieve  # type: ignore[method-assign]

        # Call on_prompt_submit with a long-enough prompt
        await hook.on_prompt_submit(
            "prompt:submit",
            {"prompt": "this is a long enough prompt to pass the length check"},
        )

        # Assert exactly one 'memory:memory_surfaced' bridge call
        surfaced = [
            (name, data)
            for name, data in coordinator.hooks._emit_log
            if name == "memory:memory_surfaced"
        ]
        assert len(surfaced) == 1, (
            f"Expected exactly one 'memory:memory_surfaced' bridge call, "
            f"got: {coordinator.hooks._emit_log}"
        )
        _, payload = surfaced[0]
        assert payload.get("ok") is True, f"Expected ok=True in payload, got: {payload}"
        assert payload.get("trigger") == "prompt_submit", (
            f"Expected trigger='prompt_submit' in payload, got: {payload}"
        )
        assert payload.get("memory_ids") == ["m1"], (
            f"Expected memory_ids=['m1'] in payload, got: {payload}"
        )


# ---------------------------------------------------------------------------
# Project-context hook — coordinator bridge tests
# ---------------------------------------------------------------------------


class TestProjectContextCoordinatorBridge:
    """Tests for coordinator bridge wiring in the project-context hooks.

    These tests verify that mount() registers a contributor and that the hooks
    emit coordinator events (coordination_read, coordination_scaffolded,
    curator_handoff_requested) via bridge_emit.
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor on the coordinator with
        channel='observability.events' and name='memory-project-context'.

        The contributor callback must return a list of events that is a superset of:
        - 'memory:coordination_read'
        - 'memory:coordination_scaffolded'
        - 'memory:curator_handoff_requested'
        """
        import asyncio

        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        coordinator = FakeCoordinator()
        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-project-context" in contribs, (
            "mount() must register contributor with name 'memory-project-context'"
        )

        callback = contribs["memory-project-context"]
        events = set(callback())
        required_events = {
            "memory:coordination_read",
            "memory:coordination_scaffolded",
            "memory:curator_handoff_requested",
        }
        assert required_events <= events, (
            f"contributor callback must include all of {required_events}, got: {events}"
        )

    def test_curator_handoff_requested_bridges_at_session_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """After a session:end event, the hook must emit exactly one
        'memory:curator_handoff_requested' bridge call when
        project-context dir exists.
        """
        import asyncio

        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        # Create a fake project-context directory so the hook proceeds
        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()

        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        bridge_calls: list[tuple[str, Any]] = []

        async def fake_bridge(event_name: str, payload: Any) -> None:
            bridge_calls.append((event_name, payload))

        hook = m.ProjectContextEndHook(bridge_emit=fake_bridge)
        asyncio.run(hook("session:end", {"session_id": "sid"}))

        handoff_calls = [
            (name, payload)
            for name, payload in bridge_calls
            if name == "memory:curator_handoff_requested"
        ]
        assert len(handoff_calls) == 1, (
            f"Expected exactly one 'memory:curator_handoff_requested' bridge call, "
            f"got: {bridge_calls}"
        )


# ---------------------------------------------------------------------------
# Tool-memory — coordinator bridge tests
# ---------------------------------------------------------------------------


class TestToolMemoryCoordinatorBridge:
    """Tests for coordinator bridge wiring in the memory tool.

    These tests verify that mount() registers a contributor and that garden
    operations forward events to the coordinator via the combined_emit /
    sync_bridge_emit bridge.
    """

    def test_register_contributor_called_at_mount(self) -> None:
        """mount() must call register_contributor on the coordinator with
        channel='observability.events' and name='memory-tool'.

        The contributor callback must return a list of events that includes:
        - 'memory:garden_completed'
        - 'memory:garden_progress'
        """
        import asyncio

        import amplifier_module_tool_memory as m  # type: ignore[import]

        coordinator = FakeCoordinator()

        asyncio.run(m.mount(coordinator))

        assert "observability.events" in coordinator._contributors, (
            "mount() must call register_contributor with channel 'observability.events'"
        )
        contribs = coordinator._contributors["observability.events"]
        assert "memory-tool" in contribs, (
            "mount() must register contributor with name 'memory-tool'"
        )

        callback = contribs["memory-tool"]
        events = callback()
        assert "memory:garden_completed" in events, (
            "contributor callback must include 'memory:garden_completed'"
        )
        assert "memory:garden_progress" in events, (
            "contributor callback must include 'memory:garden_progress'"
        )


# ---------------------------------------------------------------------------
# coordinator_bridge.py hardening — done-callback logging
# ---------------------------------------------------------------------------


class TestMakeSyncBridgeFailureLogging:
    """``make_sync_bridge`` must never let an exception raised inside the
    bridged coroutine vanish silently.

    Root cause of the native-cutover seam bug: ``run_coroutine_threadsafe``
    returns a ``concurrent.futures.Future`` whose exception is discarded
    unless something inspects it. Nothing did -- so a coroutine that failed
    (for any reason, including "no session context to resolve against")
    died with zero signal anywhere. ``make_sync_bridge`` now attaches a
    done-callback (``_log_bridge_failure``) that surfaces the exception via
    stderr and the memory-side JSONL event emitter, without changing
    ``bridge_emit``'s own never-raises contract.
    """

    def test_exception_in_bridged_coroutine_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Schedule a coroutine that raises via bridge_emit; assert the
        exception is surfaced (stderr print) rather than silently absorbed."""
        import asyncio

        from amplifier_module_tool_memory.coordinator_bridge import make_sync_bridge

        class _BoomHooks:
            async def emit(self, event_name: str, data: Any) -> None:
                raise RuntimeError("simulated bridged-coroutine failure")

        class _BoomCoordinator:
            def __init__(self) -> None:
                self.hooks = _BoomHooks()

        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
        )

        async def _main() -> None:
            coordinator = _BoomCoordinator()
            bridge_emit = make_sync_bridge(coordinator)

            # bridge_emit itself must never raise (never-raises contract).
            bridge_emit("memory:drawer_filed", {"ok": True})

            # Give the scheduled coroutine (and its done-callback) a chance
            # to run on this same loop.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if printed:
                    break

        asyncio.run(_main())

        assert printed, (
            "make_sync_bridge must log the exception from a failed bridged "
            "coroutine via its done-callback -- it must not vanish silently"
        )
        assert any("simulated bridged-coroutine failure" in line for line in printed), (
            f"Expected the actual exception message in the logged output, got: {printed}"
        )
        assert any("memory:drawer_filed" in line for line in printed), (
            f"Expected the event name in the logged output, got: {printed}"
        )

    def test_exception_in_bridged_coroutine_also_reaches_event_emitter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The done-callback must also try the module's own JSONL event
        emitter (best-effort) so the failure is visible even if stderr is
        not being watched."""
        import asyncio

        from amplifier_module_tool_memory.coordinator_bridge import make_sync_bridge

        class _BoomHooks:
            async def emit(self, event_name: str, data: Any) -> None:
                raise ValueError("boom")

        class _BoomCoordinator:
            def __init__(self) -> None:
                self.hooks = _BoomHooks()

        recorded: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            "amplifier_module_tool_memory.event_emitter.emit_event",
            lambda *a, **kw: recorded.append((a, kw)),
        )

        async def _main() -> None:
            coordinator = _BoomCoordinator()
            bridge_emit = make_sync_bridge(coordinator)
            bridge_emit("memory:drawer_filed", {"ok": True})
            for _ in range(50):
                await asyncio.sleep(0.01)
                if recorded:
                    break

        asyncio.run(_main())

        assert recorded, (
            "make_sync_bridge's done-callback must also report the failure "
            "via the module's own event emitter (best-effort)"
        )
        (args, kwargs) = recorded[0]
        assert args[0] == "coordinator_bridge"
        assert args[1] == "bridge_emit_failed"
        assert kwargs.get("ok") is False

    def test_successful_bridged_coroutine_logs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The done-callback must be a no-op when the bridged coroutine
        succeeds -- no false-positive noise."""
        import asyncio

        from amplifier_module_tool_memory.coordinator_bridge import make_sync_bridge

        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
        )

        async def _main() -> None:
            coordinator = FakeCoordinator()
            bridge_emit = make_sync_bridge(coordinator)
            bridge_emit("memory:drawer_filed", {"ok": True})
            for _ in range(20):
                await asyncio.sleep(0.01)

        asyncio.run(_main())

        assert printed == [], f"a successful bridged emit must not log anything, got: {printed}"
