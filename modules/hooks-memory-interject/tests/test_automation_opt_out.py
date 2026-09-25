"""perf/incremental-fold (part B): hooks-memory-interject must skip all
three handlers -- no daemon contact -- for automated/non-interactive runs
opted out via AMPLIFIER_MEMORY_CAPTURE or excluded_working_dirs, while
leaving a normal interactive session's default behavior unchanged.
"""

from __future__ import annotations

import asyncio

import amplifier_module_hooks_memory_interject as interject


def _fail_if_called(*_a, **_k):
    raise AssertionError("memory search must never be called when opted out")


def _run(coro):
    return asyncio.run(coro)


class TestEnvVarOptOut:
    def test_prompt_submit_skips_via_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        monkeypatch.setattr(interject, "_call_client", _fail_if_called)
        hook = interject.MemoryInterjectHook({})
        result = _run(
            hook.on_prompt_submit(
                "prompt:submit", {"session_id": "s1", "prompt": "a" * 30}
            )
        )
        assert result.action == "continue"

    def test_tool_pre_skips_via_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        monkeypatch.setattr(interject, "_call_client", _fail_if_called)
        hook = interject.MemoryInterjectHook({"tool_pre_enabled": True})
        result = _run(
            hook.on_tool_pre(
                "tool:pre",
                {
                    "session_id": "s1",
                    "tool_name": "bash",
                    "tool_input": {"command": "echo hello world long enough"},
                },
            )
        )
        assert result.action == "continue"

    def test_orchestrator_complete_skips_via_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        monkeypatch.setattr(interject, "_call_client", _fail_if_called)
        hook = interject.MemoryInterjectHook({})
        result = _run(
            hook.on_orchestrator_complete(
                "orchestrator:complete", {"session_id": "s1", "response": "a" * 60}
            )
        )
        assert result.action == "continue"


class TestExcludedWorkingDirs:
    def test_matching_cwd_glob_skips(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        monkeypatch.setattr(
            "os.getcwd", lambda: "/home/user/dev/afast-ev/bench/experiments/r1"
        )
        monkeypatch.setattr(interject, "_call_client", _fail_if_called)
        hook = interject.MemoryInterjectHook(
            {"excluded_working_dirs": ["/home/*/dev/afast-ev/*/experiments/*"]}
        )
        result = _run(
            hook.on_prompt_submit(
                "prompt:submit", {"session_id": "s1", "prompt": "a" * 30}
            )
        )
        assert result.action == "continue"


class TestEmitsSkipReason:
    def test_skip_emits_interject_skipped_automation_opt_out(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        events = []

        def fake_emit_event(component, event, *, ok, data=None, **kwargs):
            events.append((component, event, ok, data))

        monkeypatch.setattr(interject, "emit_event", fake_emit_event)
        monkeypatch.setattr(interject, "_call_client", _fail_if_called)
        hook = interject.MemoryInterjectHook({})
        _run(
            hook.on_prompt_submit(
                "prompt:submit", {"session_id": "s1", "prompt": "a" * 30}
            )
        )
        assert events == [
            (
                "memory-interject",
                "interject_skipped",
                False,
                {"trigger": "prompt_submit", "reason": "automation_opt_out"},
            )
        ]


class TestDefaultBehaviorUnchanged:
    def test_normal_session_still_searches(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        calls = []

        def fake_call_client(method, **kwargs):
            calls.append(method)
            return {"results": [], "degraded": None}

        monkeypatch.setattr(interject, "_call_client", fake_call_client)
        hook = interject.MemoryInterjectHook({})
        _run(
            hook.on_prompt_submit(
                "prompt:submit", {"session_id": "s1", "prompt": "a" * 30}
            )
        )
        assert calls == ["search"]

    def test_no_config_means_no_excluded_dirs(self) -> None:
        hook = interject.MemoryInterjectHook({})
        assert hook.excluded_working_dirs == []
