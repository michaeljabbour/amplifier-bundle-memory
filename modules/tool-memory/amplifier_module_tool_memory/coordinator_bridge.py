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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
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


def make_sync_bridge(coordinator: Any) -> SyncBridge:
    """Build a thread-safe sync bridge to ``coordinator.hooks.emit``.

    Must be called from ``async def mount`` so a running loop is
    available to capture. The returned callable is safe to invoke from
    any thread; errors and a closed loop are silently absorbed.
    """
    loop = asyncio.get_running_loop()

    def bridge_emit(event: str, payload: Any) -> None:
        try:
            if loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(
                coordinator.hooks.emit(event, payload),
                loop,
            )
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
