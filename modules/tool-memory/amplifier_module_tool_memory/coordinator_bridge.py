"""Shared coordinator bridge factories for memory modules.

One pattern, two flavors. Both swallow producer-side errors so a failing
observer never corrupts the caller.

* ``make_async_bridge`` — for callers awaiting from the coordinator's
  loop (briefing, interject, project-context hooks).
* ``make_sync_bridge``  — for callers running OFF the loop (capture's
  drain thread, tool's garden thread). Captures the running loop at
  mount time, schedules emits via ``run_coroutine_threadsafe``, guards
  against a closed loop.

``register_events`` factors out the best-effort observability
contributor registration (was inconsistently wrapped across modules).

Bridge-failure visibility (native-cutover seam fix, 2026-07):
``run_coroutine_threadsafe`` returns a ``concurrent.futures.Future`` whose
exception is silently discarded unless something inspects it. Prior to
this fix nothing did -- any exception raised inside the scheduled
``coordinator.hooks.emit(...)`` coroutine (including "no running session
context" failures) vanished with zero signal anywhere. ``make_sync_bridge``
now attaches a done-callback that logs the failure to stderr and, best
effort, to the memory-side JSONL event log -- without changing the
never-raises contract of ``bridge_emit`` itself.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future as ConcurrentFuture
from typing import Any

AsyncBridge = Callable[[str, Any], Awaitable[None]]
SyncBridge = Callable[[str, Any], None]


async def _noop_async(event: str, payload: Any) -> None:  # pragma: no cover
    return None


def _noop_sync(event: str, payload: Any) -> None:  # pragma: no cover
    return None


# Public no-op singletons. Hook/tool classes use these as defaults so
# they remain callable without going through mount() — i.e. testable.
NOOP_ASYNC_BRIDGE: AsyncBridge = _noop_async
NOOP_SYNC_BRIDGE: SyncBridge = _noop_sync


def register_events(
    coordinator: Any,
    contributor: str,
    events: Sequence[str],
) -> None:
    """Register a module's emitted event names with observability.

    Best-effort: never raises. The events list is snapshotted so the
    contributor lambda is decoupled from caller-side mutation.
    """
    try:
        snapshot = list(events)
        coordinator.register_contributor(
            "observability.events",
            contributor,
            lambda: snapshot,
        )
    except Exception:
        pass


def make_async_bridge(coordinator: Any) -> AsyncBridge:
    """Build an async bridge to ``coordinator.hooks.emit``.

    Use from within an async hook running on the coordinator's loop.
    Errors from the hook bus are swallowed.
    """

    async def bridge_emit(event: str, payload: Any) -> None:
        try:
            await coordinator.hooks.emit(event, payload)
        except Exception:
            pass

    return bridge_emit


def _log_bridge_failure(event: str, fut: ConcurrentFuture[Any]) -> None:
    """Done-callback for a ``run_coroutine_threadsafe`` future.

    Surfaces an exception that would otherwise be silently absorbed -- the
    exact failure mode that let bridged ``memory:drawer_filed`` events die
    invisibly for the lifetime of the drain-thread bridging design. Logs to
    stderr (always available) and, best-effort, to the memory-side JSONL
    event log via the module's own event emitter (so the failure is visible
    even when the coordinator's own emit path is the thing that's broken).

    Never raises -- a logging failure must not break the bridge, and this
    runs as an asyncio/concurrent.futures done-callback where an escaping
    exception would only be reported to ``sys.unraisablehook`` / a default
    exception handler, not surfaced to any caller.
    """
    try:
        exc = fut.exception()
    except BaseException:
        # Covers asyncio.CancelledError (BaseException subclass) and any
        # other exotic failure retrieving the future's outcome.
        return
    if exc is None:
        return

    message = f"[coordinator_bridge] emit({event!r}) failed: {type(exc).__name__}: {exc}"
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass

    try:
        from .event_emitter import emit_event

        emit_event(
            "coordinator_bridge",
            "bridge_emit_failed",
            ok=False,
            data={"event": event, "error": f"{type(exc).__name__}: {exc}"},
        )
    except Exception:
        pass


def make_sync_bridge(coordinator: Any) -> SyncBridge:
    """Build a thread-safe sync bridge to ``coordinator.hooks.emit``.

    Must be called from ``async def mount`` so a running loop is
    available to capture. The returned callable is safe to invoke from
    any thread; errors and a closed loop are silently absorbed by
    ``bridge_emit`` itself (never-raises contract preserved).

    A done-callback is attached to the scheduled future so exceptions
    raised inside the bridged coroutine are logged (stderr + memory-side
    JSONL) instead of vanishing -- see ``_log_bridge_failure``.
    """
    loop = asyncio.get_running_loop()

    def bridge_emit(event: str, payload: Any) -> None:
        try:
            if loop.is_closed():
                return
            fut = asyncio.run_coroutine_threadsafe(
                coordinator.hooks.emit(event, payload),
                loop,
            )
            fut.add_done_callback(lambda f, _event=event: _log_bridge_failure(_event, f))
        except Exception:
            pass

    return bridge_emit


__all__ = [
    "AsyncBridge",
    "SyncBridge",
    "NOOP_ASYNC_BRIDGE",
    "NOOP_SYNC_BRIDGE",
    "register_events",
    "make_async_bridge",
    "make_sync_bridge",
]
