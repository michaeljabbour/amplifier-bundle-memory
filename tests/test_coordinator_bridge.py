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
