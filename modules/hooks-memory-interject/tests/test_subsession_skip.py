"""
Sub-session skip for hooks-memory-interject.

This hook fires on prompt:submit, tool:pre, and orchestrator:complete for
EVERY session, including sub-agent sessions spawned via delegate(). Before
this fix, each one paid a full ensure_daemon() + MemoryClient.search() round
trip. The coordinator stamps `parent_id` onto every emitted event via
set_default_fields (amplifier_core.session.AmplifierSession) -- present here
exactly as it is on session:start, which hooks-memory-briefing already uses
to skip sub-sessions.

These tests assert NO search call is made for a sub-session event (calling
_call_client would be a test failure), for all three handlers.
"""

from __future__ import annotations

import asyncio

import amplifier_module_hooks_memory_interject as interject


def _fail_if_called(*_a, **_k):
    raise AssertionError("memory search must never be called for a sub-session event")


def test_prompt_submit_skips_subsession_without_searching(monkeypatch):
    monkeypatch.setattr(interject, "_call_client", _fail_if_called)
    hook = interject.MemoryInterjectHook({})
    result = asyncio.run(
        hook.on_prompt_submit(
            "prompt:submit",
            {
                "session_id": "child-1",
                "parent_id": "root-session",
                "prompt": "a" * 30,
            },
        )
    )
    assert result.action == "continue"


def test_tool_pre_skips_subsession_without_searching(monkeypatch):
    monkeypatch.setattr(interject, "_call_client", _fail_if_called)
    hook = interject.MemoryInterjectHook({"tool_pre_enabled": True})
    result = asyncio.run(
        hook.on_tool_pre(
            "tool:pre",
            {
                "session_id": "child-1",
                "parent_id": "root-session",
                "tool_name": "bash",
                "tool_input": {"command": "echo hello world this is long enough"},
            },
        )
    )
    assert result.action == "continue"


def test_orchestrator_complete_skips_subsession_without_searching(monkeypatch):
    monkeypatch.setattr(interject, "_call_client", _fail_if_called)
    hook = interject.MemoryInterjectHook({})
    result = asyncio.run(
        hook.on_orchestrator_complete(
            "orchestrator:complete",
            {
                "session_id": "child-1",
                "parent_id": "root-session",
                "response": "a" * 60,
            },
        )
    )
    assert result.action == "continue"


def test_top_level_session_with_no_parent_id_still_searches(monkeypatch):
    """Sanity check: the skip must be keyed on parent_id being PRESENT and
    non-None, not on any other property -- a normal top-level session (no
    parent_id, or parent_id=None) must still search as before."""
    calls = []

    def fake_call_client(method, **kwargs):
        calls.append(method)
        return {"results": [], "degraded": None}

    monkeypatch.setattr(interject, "_call_client", fake_call_client)
    hook = interject.MemoryInterjectHook({})
    asyncio.run(
        hook.on_prompt_submit(
            "prompt:submit",
            {"session_id": "top-level", "parent_id": None, "prompt": "a" * 30},
        )
    )
    assert calls == ["search"]


def test_skip_emits_interject_skipped_with_sub_session_reason(monkeypatch):
    events = []

    def fake_emit_event(component, event, *, ok, data=None, **kwargs):
        events.append((component, event, ok, data))

    monkeypatch.setattr(interject, "emit_event", fake_emit_event)
    monkeypatch.setattr(interject, "_call_client", _fail_if_called)

    hook = interject.MemoryInterjectHook({})
    asyncio.run(
        hook.on_prompt_submit(
            "prompt:submit",
            {"session_id": "child-1", "parent_id": "root", "prompt": "a" * 30},
        )
    )
    assert events == [
        (
            "memory-interject",
            "interject_skipped",
            False,
            {"trigger": "prompt_submit", "reason": "sub_session"},
        )
    ]


def test_skip_respects_emit_events_false(monkeypatch):
    events = []

    def fake_emit_event(*a, **k):
        events.append((a, k))

    monkeypatch.setattr(interject, "emit_event", fake_emit_event)
    monkeypatch.setattr(interject, "_call_client", _fail_if_called)

    hook = interject.MemoryInterjectHook({"emit_events": False})
    asyncio.run(
        hook.on_prompt_submit(
            "prompt:submit",
            {"session_id": "child-1", "parent_id": "root", "prompt": "a" * 30},
        )
    )
    assert events == []
