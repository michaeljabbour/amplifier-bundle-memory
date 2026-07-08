"""Tests for the native-cutover coordinator-bridge seam fix.

Root cause (DTU-validated, pinned): ``memory:drawer_filed`` bridged from the
drain thread (``_DRAIN_BRIDGE`` called inside ``_process_job``/``_drain_loop``)
never once appeared in ANY session's ``events.jsonl``. The bridge call used
``asyncio.run_coroutine_threadsafe`` from a foreign thread with no session
context, and the scheduled coroutine's outcome (success or exception) was
never inspected -- it died invisibly.

The fix moves the bridge call onto the hot path's event loop and context:
a per-job ``asyncio.Future`` is created in ``__call__`` (or replay), a task
is scheduled there to await it, and the drain thread only ever resolves the
future via ``loop.call_soon_threadsafe`` -- it never bridges directly.

These tests simulate the REAL flow: a hook invoked on a persistent, running
event loop (not ``asyncio.run()``, which tears the loop down immediately),
a real background drain thread, and a real ``asyncio.Future`` round-trip
between threads.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal FakeCoordinator/FakeHooks -- same shape as tests/test_coordinator_bridge.py
# at the repo root, kept local here so this module's test suite is self-contained.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, action: str = "continue") -> None:
        self.action = action


class FakeHooks:
    """Stub of ``coordinator.hooks`` (RustHookRegistry) good enough to drive
    ``make_sync_bridge``'s ``run_coroutine_threadsafe`` round-trip for real."""

    def __init__(self) -> None:
        self._registered: dict[str, list[tuple[Any, str, int]]] = {}
        self._emit_log: list[tuple[str, Any]] = []
        self._emit_log_lock = threading.Lock()
        # Records the running-loop identity at each emit() call, so tests
        # can assert the bridge call really happened on the target loop.
        self.emit_loops: list[asyncio.AbstractEventLoop] = []

    def register(self, event_name: str, handler: Any, name: str = "", priority: int = 0) -> None:
        self._registered.setdefault(event_name, []).append((handler, name, priority))

    async def emit(self, event_name: str, data: Any = None) -> _Result:
        with self._emit_log_lock:
            self._emit_log.append((event_name, data))
        self.emit_loops.append(asyncio.get_running_loop())
        for handler, _name, _priority in self._registered.get(event_name, []):
            result = handler(event_name, data)
            if asyncio.iscoroutine(result):
                await result
        return _Result(action="continue")


class FakeCoordinator:
    def __init__(self) -> None:
        self.hooks: FakeHooks = FakeHooks()
        self._contributors: dict[str, dict[str, Any]] = {}

    def register_contributor(self, channel: str, name: str, callback: Any) -> None:
        self._contributors.setdefault(channel, {})[name] = callback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drain_capture_queue_between_tests() -> Any:
    """Ensure each test starts and ends with an empty drain queue (module-level
    singleton -- a job left in flight could bleed into the next test)."""
    yield
    import amplifier_module_hooks_memory_capture as m

    if m._QUEUE is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and m._QUEUE.unfinished_tasks > 0:
            time.sleep(0.01)


async def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    """Poll *predicate* on the running loop until True or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Real-flow test: hook on the loop, drain thread in the background, bridge
# call happens from the loop/session context.
# ---------------------------------------------------------------------------


async def _run_real_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> dict[str, Any]:
    import amplifier_module_hooks_memory_capture as m

    monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
    monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))
    monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

    coordinator = FakeCoordinator()
    await m.mount(coordinator)

    # Grab the actually-mounted hook (bound to the coordinator's bridge_emit)
    # via the registered tool:post handler -- mirrors the pattern used for
    # the interject hook in tests/test_coordinator_bridge.py.
    handlers = coordinator.hooks._registered.get("tool:post", [])
    assert handlers, "Expected tool:post handler registered after mount()"
    hook = handlers[0][0]  # registered callable IS the hook instance (not a bound method)

    result = await hook(
        "tool:post",
        {
            "tool_name": "bash",
            "tool_input": {"command": "echo hi"},
            "tool_output": "x" * 200,
            "session_id": "sess-real-flow",
        },
    )
    assert result.action == "continue"

    def _bridged() -> bool:
        return any(ev == "memory:drawer_filed" for ev, _ in coordinator.hooks._emit_log)

    ok = await _wait_until(_bridged, timeout=5.0)
    assert ok, (
        f"memory:drawer_filed never reached coordinator.hooks._emit_log: "
        f"{coordinator.hooks._emit_log}"
    )

    _, payload = next(
        (ev, data) for ev, data in coordinator.hooks._emit_log if ev == "memory:drawer_filed"
    )
    return {"payload": payload, "emit_loops": coordinator.hooks.emit_loops}


def test_drawer_filed_reaches_coordinator_with_session_id_from_loop_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The killer assertion: memory:drawer_filed, bridged from the drain
    thread's completion signal via the hot-path's future+task, actually
    reaches coordinator.hooks._emit_log -- carrying session_id -- and the
    bridge call itself ran on the SAME loop the hook was invoked from (i.e.
    from proper session/context, not a foreign thread with none)."""
    the_loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    async def _main() -> dict[str, Any]:
        the_loop_holder["loop"] = asyncio.get_running_loop()
        return await _run_real_flow(monkeypatch, tmp_path)

    outcome = asyncio.run(_main())

    payload = outcome["payload"]
    assert payload.get("session_id") == "sess-real-flow", (
        f"bridged drawer_filed payload must carry session_id, got: {payload}"
    )
    assert payload.get("ok") is True
    assert payload.get("capture_id")

    # The bridge's coordinator.hooks.emit() call ran on the SAME loop the
    # hook itself was invoked from -- proof the emission happened from the
    # hot-path's context/loop, not an orphaned foreign-thread coroutine.
    assert outcome["emit_loops"], "coordinator.hooks.emit() was never called"
    assert all(loop is the_loop_holder["loop"] for loop in outcome["emit_loops"]), (
        "coordinator.hooks.emit() ran on a different loop than the hot path -- "
        "the bridge is not running from proper session context"
    )


def test_capture_failed_reaches_coordinator_on_drawer_write_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Mirror test for the failure path: _file_drawer raising must bridge
    memory:capture_failed (not drawer_filed) to the coordinator, also
    carrying session_id."""
    import amplifier_module_hooks_memory_capture as m

    def _boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("daemon unavailable")

    async def _main() -> dict[str, Any]:
        monkeypatch.setattr(m, "_file_drawer", _boom)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        coordinator = FakeCoordinator()
        await m.mount(coordinator)
        handlers = coordinator.hooks._registered.get("tool:post", [])
        hook = handlers[0][0]  # registered callable IS the hook instance (not a bound method)

        await hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "x" * 200,
                "session_id": "sess-failure",
            },
        )

        def _bridged() -> bool:
            return any(ev == "memory:capture_failed" for ev, _ in coordinator.hooks._emit_log)

        ok = await _wait_until(_bridged, timeout=5.0)
        assert ok, (
            f"memory:capture_failed never reached the coordinator: {coordinator.hooks._emit_log}"
        )
        _, payload = next(
            (ev, data) for ev, data in coordinator.hooks._emit_log if ev == "memory:capture_failed"
        )
        return payload

    payload = asyncio.run(_main())
    assert payload.get("session_id") == "sess-failure"
    assert payload.get("reason") == "mcp_error"


# ---------------------------------------------------------------------------
# Session-ends-early cancellation path
# ---------------------------------------------------------------------------


def test_session_ending_early_cancels_bridge_gracefully_no_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """If the awaiting bridge task is cancelled before the drain thread
    resolves the future (simulating the session ending first), nothing
    crashes -- the drain thread still runs to completion and the
    memory-side JSONL log still gets the real outcome.  There is simply no
    coordinator-bridged event for that capture; that's fine."""
    import amplifier_module_hooks_memory_capture as m

    private_emits: list[tuple[Any, ...]] = []
    emit_lock = threading.Lock()

    def _capture(*a: Any, **kw: Any) -> None:
        with emit_lock:
            private_emits.append((a, kw))

    file_drawer_called = threading.Event()

    def _slow_file_drawer(*a: Any, **kw: Any) -> None:
        # Give the test time to cancel the bridge task before the drain
        # thread resolves the future.
        file_drawer_called.set()
        time.sleep(0.2)

    async def _main() -> None:
        monkeypatch.setattr(m, "_file_drawer", _slow_file_drawer)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))
        monkeypatch.setattr(m, "emit_event", _capture)

        coordinator = FakeCoordinator()
        await m.mount(coordinator)
        handlers = coordinator.hooks._registered.get("tool:post", [])
        hook = handlers[0][0]  # registered callable IS the hook instance (not a bound method)

        assert m._PENDING_BRIDGE_TASKS == set()

        await hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "x" * 200,
                "session_id": "sess-cancel-early",
            },
        )

        # Exactly one bridge task should now be pending -- cancel it, as if
        # the session ended before the drain thread finished.
        pending = list(m._PENDING_BRIDGE_TASKS)
        assert len(pending) == 1, f"expected one pending bridge task, got {pending}"
        pending[0].cancel()

        # Let the cancellation propagate and the drain thread finish.
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline and (
            m._QUEUE is None or m._QUEUE.unfinished_tasks > 0
        ):
            await asyncio.sleep(0.01)

        # Give the cancelled task a moment to actually finish running so it
        # is removed from _PENDING_BRIDGE_TASKS by its done-callback.
        await asyncio.sleep(0.05)

    # Must not raise -- this is the whole point of the test.
    asyncio.run(_main())

    # The memory-side JSONL log must still have recorded the real outcome
    # even though the coordinator bridge was cancelled.
    filed = [e for e in private_emits if e[0][1] == "drawer_filed"]
    assert len(filed) == 1, (
        f"memory-side JSONL log must still record drawer_filed even when "
        f"the coordinator bridge task was cancelled early, got: {private_emits}"
    )


def test_emit_events_false_never_creates_a_bridge_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """emit_events=False must mean no completion future/task is ever wired
    -- confirms _wire_completion_bridge's no-op path."""
    import amplifier_module_hooks_memory_capture as m

    async def _main() -> None:
        monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x"))
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        coordinator = FakeCoordinator()
        await m.mount(coordinator, config={"emit_events": False})
        handlers = coordinator.hooks._registered.get("tool:post", [])
        hook = handlers[0][0]  # registered callable IS the hook instance (not a bound method)

        await hook(
            "tool:post",
            {
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "x" * 200,
                "session_id": "sess-no-events",
            },
        )

        assert m._PENDING_BRIDGE_TASKS == set(), (
            "emit_events=False must never create a coordinator-bridge task"
        )

        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline and (
            m._QUEUE is None or m._QUEUE.unfinished_tasks > 0
        ):
            await asyncio.sleep(0.01)

    asyncio.run(_main())
