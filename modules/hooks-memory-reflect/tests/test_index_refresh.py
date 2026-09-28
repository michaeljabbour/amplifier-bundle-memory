"""T3.2 (D19) -- hooks-memory-reflect's opt-in ``index_refresh`` trigger:
spawns ``memory:curator`` at session:start when a room's L3 index has
drifted past ``index_refresh_min_pending``, or a standing question is
stale. Off by default; capability-checked (never a hard dependency).
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_reflect as reflect_mod
import pytest
from amplifier_module_hooks_memory_reflect import MemoryReflectHook


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeHookRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple[str, Any, str]] = []

    def register(self, event: str, hook: Any, *, name: str) -> None:
        self.registered.append((event, hook, name))


class FakeSpawn:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("spawn boom")
        return {"output": "done", "session_id": "child-session-1"}


class FakeCoordinator:
    def __init__(self, *, parent_id: str | None = None, spawn_fn: Any = None) -> None:
        self.parent_id = parent_id
        self.session = object()
        self.config: dict[str, Any] = {"agents": {}}
        self.hooks = FakeHookRegistry()
        self._capabilities: dict[str, Any] = {}
        if spawn_fn is not None:
            self._capabilities["session.spawn"] = spawn_fn
        self._cleanups: list[Any] = []

    def get(self, mount_point: str, name: str | None = None) -> Any:
        return None

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_cleanup(self, cleanup_fn: Any) -> None:
        self._cleanups.append(cleanup_fn)


class FakeIndexClient:
    """Shaped like MemoryClient with the P3 L3 ops."""

    def __init__(
        self,
        index_rows: list[dict[str, Any]] | None = None,
        standing_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._index_rows = index_rows or []
        self._standing_rows = standing_rows or []

    def index(self, *, wing: str, room: str | None = None) -> list[dict[str, Any]]:
        return self._index_rows

    def standing(self, *, wing: str) -> list[dict[str, Any]]:
        return self._standing_rows


class UnsupportedClient:
    """Older tool-memory pin: no L3 ops at all."""


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def _fake_emit(
        hook: str,
        event: str,
        *,
        ok: bool = True,
        preview: str | None = None,
        data: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        recorded.append({"hook": hook, "event": event, "ok": ok, "data": data or {}})

    monkeypatch.setattr(reflect_mod, "emit_event", _fake_emit)
    return recorded


def _make_hook(
    monkeypatch: pytest.MonkeyPatch,
    client: Any,
    coordinator: FakeCoordinator,
    **config: Any,
) -> MemoryReflectHook:
    monkeypatch.setattr(reflect_mod, "ensure_daemon", lambda: client)
    monkeypatch.setattr(reflect_mod, "_detect_wing", lambda *a, **k: "wing_test")
    hook = MemoryReflectHook(config)
    hook._coordinator = coordinator
    return hook


async def _fire(hook: MemoryReflectHook, event: str, data: dict[str, Any]) -> None:
    await hook(event, data)
    pending = list(hook._pending_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestOffByDefault:
    def test_index_refresh_disabled_by_default_never_calls_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeIndexClient(
            index_rows=[
                {"scope": "room:r", "source": "derived", "pending_changes": 999}
            ]
        )
        # Make client.index a spy so we can assert it was never called.
        calls = {"n": 0}
        real_index = client.index

        def spy_index(**kw: Any) -> Any:
            calls["n"] += 1
            return real_index(**kw)

        client.index = spy_index  # type: ignore[method-assign]

        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        hook = _make_hook(monkeypatch, client, coordinator, drain_on_start=False)
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert calls["n"] == 0
        assert spawn.calls == []


class TestCapabilityCheck:
    def test_store_unsupported_skips_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = UnsupportedClient()
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        events = _events(monkeypatch)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert spawn.calls == []
        skipped = [e for e in events if e["event"] == "memory:index_refresh_skipped"]
        assert skipped
        assert skipped[0]["data"]["reason"] == "store_unsupported"


class TestSpawnConditions:
    def test_nothing_stale_skips_without_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeIndexClient(
            index_rows=[{"scope": "room:r", "source": "curated", "pending_changes": 0}],
            standing_rows=[{"question_ref": "q1", "stale": False}],
        )
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        events = _events(monkeypatch)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert spawn.calls == []
        skipped = [e for e in events if e["event"] == "memory:index_refresh_skipped"]
        assert any(e["data"].get("reason") == "nothing_stale" for e in skipped)

    def test_stale_room_spawns_curator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeIndexClient(
            index_rows=[
                {"scope": "room:r", "source": "derived", "pending_changes": 15}
            ],
            standing_rows=[],
        )
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        events = _events(monkeypatch)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
            index_refresh_min_pending=10,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["agent_name"] == "memory:curator"
        assert "r" in spawn.calls[0]["instruction"]
        queued = [e for e in events if e["event"] == "memory:index_refresh_queued"]
        assert queued
        assert queued[0]["data"]["rooms"] == ["r"]

    def test_below_min_pending_does_not_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeIndexClient(
            index_rows=[{"scope": "room:r", "source": "derived", "pending_changes": 3}],
        )
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
            index_refresh_min_pending=10,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert spawn.calls == []

    def test_stale_standing_question_spawns_curator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeIndexClient(
            index_rows=[],
            standing_rows=[{"question_ref": "q1", "stale": True}],
        )
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert len(spawn.calls) == 1
        assert "q1" in spawn.calls[0]["instruction"]

    def test_no_spawn_capability_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeIndexClient(
            index_rows=[
                {"scope": "room:r", "source": "derived", "pending_changes": 15}
            ],
        )
        coordinator = FakeCoordinator(spawn_fn=None)
        events = _events(monkeypatch)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        skipped = [e for e in events if e["event"] == "memory:index_refresh_skipped"]
        assert any(e["data"].get("reason") == "no_spawn_capability" for e in skipped)

    def test_spawn_cap_not_shared_with_reflection_spawns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """index_refresh uses its own spawn call, independent of
        max_spawns_per_session (which only gates distiller spawns)."""
        client = FakeIndexClient(
            index_rows=[
                {"scope": "room:r", "source": "derived", "pending_changes": 15}
            ],
        )
        spawn = FakeSpawn()
        coordinator = FakeCoordinator(spawn_fn=spawn)
        hook = _make_hook(
            monkeypatch,
            client,
            coordinator,
            drain_on_start=False,
            index_refresh=True,
            max_spawns_per_session=0,
        )
        _run(_fire(hook, "session:start", {"session_id": "s1"}))
        assert len(spawn.calls) == 1
