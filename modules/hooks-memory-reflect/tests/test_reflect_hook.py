"""Unit tests for hooks-memory-reflect (T2.5).

All coordinator/context/store/spawn dependencies are fakes -- no real
amplifier-core session, no real memory daemon. tool-memory's real
``reflection_job_add``/``reflection_jobs`` ops are being added concurrently
in a sibling change; these tests exercise this hook's contract against a
fake client shaped like the interface it was given, plus a client that is
missing those methods (the ``store_unsupported`` path).
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_reflect as reflect_mod
import pytest
from amplifier_module_hooks_memory_reflect import (
    MemoryReflectHook,
    _flatten_content,
    _is_memory_injection,
    _render_span,
    mount,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeContext:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)


class FakeHookRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple[str, Any, str]] = []

    def register(self, event: str, hook: Any, *, name: str) -> None:
        self.registered.append((event, hook, name))


class FakeCoordinator:
    def __init__(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
        spawn_fn: Any = None,
    ) -> None:
        self.parent_id = parent_id
        self.session = object()
        self.config: dict[str, Any] = {"agents": {}}
        self.hooks = FakeHookRegistry()
        self._context = FakeContext(messages or [])
        self._spawn_fn = spawn_fn
        self._capabilities: dict[str, Any] = {}
        if spawn_fn is not None:
            self._capabilities["session.spawn"] = spawn_fn
        self._cleanups: list[Any] = []
        self.contributors: list[tuple[str, str]] = []

    def get(self, mount_point: str, name: str | None = None) -> Any:
        if mount_point == "context":
            return self._context
        return None

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def register_cleanup(self, cleanup_fn: Any) -> None:
        self._cleanups.append(cleanup_fn)

    def register_contributor(self, mount_point: str, contributor: str, fn: Any) -> None:
        self.contributors.append((mount_point, contributor))


class FakeSpawn:
    """Records every call; returns a canned result."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("spawn boom")
        return {"output": "done", "session_id": "child-session-1"}


class FakeClient:
    """Shaped like MemoryClient's reflection surface (T2.5 ops)."""

    def __init__(self) -> None:
        self.jobs_added: list[dict[str, Any]] = []
        self._next_ref = 0
        self.pending_jobs: list[dict[str, Any]] = []
        self.raise_on_add = False
        self.raise_on_list = False

    def reflection_job_add(self, **kwargs: Any) -> dict[str, Any]:
        if self.raise_on_add:
            raise RuntimeError("store boom")
        self._next_ref += 1
        job_ref = f"job-{self._next_ref}"
        self.jobs_added.append({**kwargs, "job_ref": job_ref})
        return {
            "job_ref": job_ref,
            "span_ref": f"span-{self._next_ref}",
            "state": "pending",
            "redactions": {},
        }

    def reflection_jobs(
        self, *, state: str = "pending", wing: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        if self.raise_on_list:
            raise RuntimeError("store boom")
        return self.pending_jobs[:limit]


class UnsupportedClient:
    """A client shaped like an older tool-memory daemon: no reflection ops."""


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch emit_event to a recorder and return the list it appends to."""
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


def _msg(role: str, content: Any) -> dict[str, Any]:
    return {"role": role, "content": content}


def _conversation(n_pairs: int) -> list[dict[str, Any]]:
    """n_pairs user+assistant messages (2*n_pairs total, all non-system)."""
    out: list[dict[str, Any]] = []
    for i in range(n_pairs):
        out.append(_msg("user", f"question {i}"))
        out.append(_msg("assistant", f"answer {i}"))
    return out


async def _fire(hook: MemoryReflectHook, event: str, data: dict[str, Any]) -> None:
    """Invoke the hook, then await every task it spawned (test convenience --
    the hook itself never awaits its own tasks, by design)."""
    await hook(event, data)
    pending = list(hook._pending_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_flatten_content_handles_str_none_and_list_blocks() -> None:
    assert _flatten_content("hello") == "hello"
    assert _flatten_content(None) == ""
    assert _flatten_content([{"type": "text", "text": "a"}, "b"]) == "a\nb"


def test_is_memory_injection_matches_briefing_and_interject_markers() -> None:
    assert _is_memory_injection("## Memory Briefing -- `wing_x`\nstuff")
    assert _is_memory_injection(
        "\U0001f4da Relevant memory from a previous session:\n- a fact"
    )
    assert not _is_memory_injection("just a normal message")


def test_render_span_skips_system_and_injections_truncates_tool_and_caps_span() -> None:
    messages = [
        _msg("system", "you are an assistant"),
        _msg("system", "## Memory Briefing -- `wing_x`\ninjected briefing content"),
        _msg("user", "please read this file"),
        _msg("tool", "x" * 5000),
        _msg("assistant", "ok, done"),
    ]
    rendered = _render_span(messages, max_tool_result_chars=100, max_span_chars=100000)
    assert "[system]" not in rendered
    assert "injected briefing" not in rendered
    assert "[tool] " + "x" * 100 + "...[truncated]" in rendered
    assert "[user] please read this file" in rendered
    assert "[assistant] ok, done" in rendered


def test_render_span_caps_whole_span_keeping_newest_content() -> None:
    messages = [_msg("user", f"msg-{i}" * 20) for i in range(50)]
    rendered = _render_span(messages, max_tool_result_chars=100000, max_span_chars=200)
    assert len(rendered) <= 200 + len("...[earlier content truncated]...\n")
    # Newest content (highest index) must be present; oldest must be gone.
    assert "msg-49" in rendered
    assert "msg-0msg-0" not in rendered  # the very first message's repeated token


# ---------------------------------------------------------------------------
# Watermark / gating
# ---------------------------------------------------------------------------


def test_watermark_slicing_and_no_double_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    messages = _conversation(5)  # 10 non-system messages
    coordinator = FakeCoordinator(messages=messages)
    _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=6)

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))
    assert len(client.jobs_added) == 1
    assert client.jobs_added[0]["span_text"].count("[user]") == 5

    # Firing again with NO new messages must not re-queue the same span.
    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))
    assert len(client.jobs_added) == 1

    # New messages arrive -- only the delta since the watermark is reflected.
    coordinator._context.messages.extend(_conversation(3))
    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))
    assert len(client.jobs_added) == 2
    assert client.jobs_added[1]["span_text"].count("[user]") == 3


def test_too_small_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    coordinator = FakeCoordinator(messages=_conversation(1))  # 2 messages
    events = _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=6)

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    assert client.jobs_added == []
    skipped = [e for e in events if e["event"] == "memory:reflection_skipped"]
    assert skipped and skipped[0]["data"]["reason"] == "too_small"


# ---------------------------------------------------------------------------
# Child-session guard
# ---------------------------------------------------------------------------


def test_child_session_no_ops_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    coordinator = FakeCoordinator(
        messages=_conversation(10), parent_id="parent-session-1"
    )
    events = _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "context:compaction", {"session_id": "child-1"}))

    assert client.jobs_added == []
    assert events == []
    assert hook._pending_tasks == set()


# ---------------------------------------------------------------------------
# session:end vs compaction spawning
# ---------------------------------------------------------------------------


def test_session_end_queues_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=_conversation(10), spawn_fn=spawn)
    _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "session:end", {"session_id": "s1"}))

    assert len(client.jobs_added) == 1
    assert spawn.calls == []


def test_compaction_spawns_once_with_correct_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=_conversation(10), spawn_fn=spawn)
    _events(monkeypatch)
    hook = _make_hook(
        monkeypatch, client, coordinator, min_new_messages=1, agent="memory:distiller"
    )

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    assert len(client.jobs_added) == 1
    job_ref = client.jobs_added[0]["job_ref"]
    assert len(spawn.calls) == 1
    call = spawn.calls[0]
    assert call["agent_name"] == "memory:distiller"
    assert job_ref in call["instruction"]
    assert "reflection_job_done" in call["instruction"]
    assert call["parent_session"] is coordinator.session


def test_pre_compact_also_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=_conversation(10), spawn_fn=spawn)
    _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "context:pre_compact", {"session_id": "s1"}))

    assert len(spawn.calls) == 1


# ---------------------------------------------------------------------------
# Spawn cap
# ---------------------------------------------------------------------------


def test_spawn_cap_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=_conversation(2), spawn_fn=spawn)
    events = _events(monkeypatch)
    hook = _make_hook(
        monkeypatch,
        client,
        coordinator,
        min_new_messages=1,
        max_spawns_per_session=1,
    )

    # Two separate compaction events, each with new messages, so each queues
    # a job -- but only the first may spawn.
    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))
    coordinator._context.messages.extend(_conversation(2))
    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    assert len(client.jobs_added) == 2
    assert len(spawn.calls) == 1
    skipped = [
        e
        for e in events
        if e["event"] == "memory:reflection_skipped"
        and e["data"].get("reason") == "spawn_cap_reached"
    ]
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# No spawn capability
# ---------------------------------------------------------------------------


def test_no_spawn_capability_leaves_job_pending_and_emits_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    coordinator = FakeCoordinator(messages=_conversation(10), spawn_fn=None)
    events = _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    # Job WAS queued (durable) even though nothing could process it yet.
    assert len(client.jobs_added) == 1
    skipped = [
        e
        for e in events
        if e["event"] == "memory:reflection_skipped"
        and e["data"].get("reason") == "no_spawn_capability"
    ]
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# store_unsupported
# ---------------------------------------------------------------------------


def test_store_unsupported_skips_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    client = UnsupportedClient()
    coordinator = FakeCoordinator(messages=_conversation(10))
    events = _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    skipped = [
        e
        for e in events
        if e["event"] == "memory:reflection_skipped"
        and e["data"].get("reason") == "store_unsupported"
    ]
    assert len(skipped) == 1


def test_store_error_on_add_does_not_advance_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    client.raise_on_add = True
    coordinator = FakeCoordinator(messages=_conversation(10))
    events = _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, min_new_messages=1)

    _run(_fire(hook, "context:compaction", {"session_id": "s1"}))

    assert hook._watermark == 0
    failed = [e for e in events if e["event"] == "memory:reflection_failed"]
    assert failed and failed[0]["data"]["reason"] == "store_error"


# ---------------------------------------------------------------------------
# Drain on start
# ---------------------------------------------------------------------------


def test_drain_on_start_spawns_pending_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    client.pending_jobs = [
        {"job_ref": "job-a"},
        {"job_ref": "job-b"},
        {"job_ref": "job-c"},
    ]
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=[], spawn_fn=spawn)
    _events(monkeypatch)
    hook = _make_hook(
        monkeypatch, client, coordinator, drain_on_start=True, drain_limit=3
    )

    _run(_fire(hook, "session:start", {"session_id": "s1"}))

    assert len(spawn.calls) == 3
    refs = {c["instruction"].split()[3].rstrip(".") for c in spawn.calls}
    assert refs == {"job-a", "job-b", "job-c"}


def test_drain_on_start_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    client.pending_jobs = [{"job_ref": "job-a"}]
    spawn = FakeSpawn()
    coordinator = FakeCoordinator(messages=[], spawn_fn=spawn)
    _events(monkeypatch)
    hook = _make_hook(monkeypatch, client, coordinator, drain_on_start=False)

    _run(_fire(hook, "session:start", {"session_id": "s1"}))

    assert spawn.calls == []


# ---------------------------------------------------------------------------
# mount() + cleanup
# ---------------------------------------------------------------------------


def test_mount_registers_handlers_and_cleanup_awaits_then_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    coordinator = FakeCoordinator(messages=_conversation(10))
    monkeypatch.setattr(reflect_mod, "ensure_daemon", lambda: client)
    monkeypatch.setattr(reflect_mod, "_detect_wing", lambda *a, **k: "wing_test")
    _events(monkeypatch)

    async def _do_mount() -> tuple[dict[str, Any], MemoryReflectHook]:
        meta = await mount(coordinator, {"min_new_messages": 1, "drain_timeout_s": 1})
        # The hook instance is whatever got registered for context:compaction.
        hook = next(
            h
            for (ev, h, _name) in coordinator.hooks.registered
            if ev == "context:compaction"
        )
        return meta, hook

    async def _scenario() -> None:
        meta, hook = await _do_mount()

        assert meta["name"] == "hooks-memory-reflect"
        registered_events = {ev for (ev, _h, _n) in coordinator.hooks.registered}
        assert registered_events == {
            "context:compaction",
            "context:pre_compact",
            "session:end",
            "session:start",
        }
        assert len(coordinator._cleanups) == 1
        assert (
            "observability.events",
            "hooks-memory-reflect",
        ) in coordinator.contributors

        # Fire a real event through the mounted hook so a task is in flight,
        # then invoke the registered cleanup: it must await completion (the
        # fake client resolves immediately, so nothing is left to cancel).
        await hook("context:compaction", {"session_id": "s1"})
        assert len(hook._pending_tasks) == 1

        cleanup_fn = coordinator._cleanups[0]
        await cleanup_fn()

        assert len(client.jobs_added) == 1
        assert hook._pending_tasks == set()

    _run(_scenario())


def test_cleanup_cancels_stragglers_past_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator(messages=_conversation(10))
    hook = MemoryReflectHook({"drain_timeout_s": 0.05})
    hook._coordinator = coordinator

    async def _never_finishes() -> None:
        await asyncio.sleep(10)

    async def _scenario() -> None:
        task = asyncio.ensure_future(_never_finishes())
        hook._track(task)

        async def cleanup() -> None:
            pending = list(hook._pending_tasks)
            if not pending:
                return
            _, still_pending = await asyncio.wait(pending, timeout=hook.drain_timeout_s)
            for t in still_pending:
                t.cancel()

        await cleanup()
        assert task.cancelled() or task.cancelling() > 0

    _run(_scenario())
