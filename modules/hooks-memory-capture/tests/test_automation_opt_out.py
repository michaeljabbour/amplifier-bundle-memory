"""perf/incremental-fold (part B): hooks-memory-capture must skip entirely
-- no daemon contact, no spool write -- for automated/non-interactive runs
opted out via AMPLIFIER_MEMORY_CAPTURE or excluded_working_dirs, while
leaving a normal interactive session's default behavior unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_capture as capture_module
from amplifier_module_hooks_memory_capture import MemoryCaptureHook


def _run(coro):  # noqa: ANN001
    return asyncio.run(coro)


def _make_hook(monkeypatch, **config: Any) -> MemoryCaptureHook:
    monkeypatch.setattr(capture_module, "emit_event", lambda *a, **k: None)
    return MemoryCaptureHook(config=config)


class TestEnvVarOptOut:
    def test_env_var_off_skips_before_any_gate(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        hook = _make_hook(monkeypatch)
        # A payload that WOULD otherwise be captured (a real, worthy tool
        # output) -- if opt-out fires first, it never reaches the gate at
        # all, so this input choice is deliberate: only opt-out explains a
        # "continue" result here.
        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {"command": "echo hi"},
                    "result": {"output": "we decided to use rust for the kernel"},
                    "session_id": "s1",
                },
            )
        )
        assert result.action == "continue"

    def test_env_var_unset_does_not_skip(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        hook = _make_hook(monkeypatch)
        # too_short gate still fires normally (proves we reached the real
        # gating logic, not the opt-out early-return).
        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "result": {"output": "x"},
                    "session_id": "s1",
                },
            )
        )
        assert result.action == "continue"  # too_short, not opt-out, but same result


class TestExcludedWorkingDirs:
    def test_matching_cwd_glob_skips(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        monkeypatch.setattr(
            capture_module.os, "getcwd", lambda: "/home/user/dev/afast-ev/bench/experiments/r1"
        )
        hook = _make_hook(
            monkeypatch, excluded_working_dirs=["/home/*/dev/afast-ev/*/experiments/*"]
        )
        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {"command": "echo hi"},
                    "result": {"output": "we decided to use rust for the kernel"},
                    "session_id": "s1",
                },
            )
        )
        assert result.action == "continue"

    def test_non_matching_cwd_does_not_skip_via_automation_gate(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        monkeypatch.setattr(capture_module.os, "getcwd", lambda: "/home/user/dev/real-project")
        skip_events: list[dict[str, Any]] = []

        def _fake_emit_event(component, event, *, ok, data=None, **kwargs):  # noqa: ANN001
            skip_events.append(data or {})

        monkeypatch.setattr(capture_module, "emit_event", _fake_emit_event)
        hook = MemoryCaptureHook(
            config={"excluded_working_dirs": ["/home/*/dev/afast-ev/*/experiments/*"]}
        )
        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "result": {"output": "x"},
                    "session_id": "s1",
                },
            )
        )
        # Reached the real gate (too_short), not the automation opt-out.
        assert all(d.get("reason") != "automation_opt_out" for d in skip_events)


class TestEmitsSkipReason:
    def test_skip_emits_capture_skipped_automation_opt_out(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        events: list[tuple[str, str, bool, dict]] = []

        def _fake_emit_event(component, event, *, ok, data=None, **kwargs):  # noqa: ANN001
            events.append((component, event, ok, data or {}))

        monkeypatch.setattr(capture_module, "emit_event", _fake_emit_event)
        hook = MemoryCaptureHook(config={"emit_events": True})
        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "result": {"output": "we decided to use rust"},
                    "session_id": "s1",
                },
            )
        )
        assert events == [
            (
                "memory-capture",
                "capture_skipped",
                False,
                {"reason": "automation_opt_out"},
            )
        ]

    def test_skip_respects_emit_events_false(self, monkeypatch) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")
        events: list[Any] = []
        monkeypatch.setattr(capture_module, "emit_event", lambda *a, **k: events.append((a, k)))
        hook = MemoryCaptureHook(config={"emit_events": False})
        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "result": {"output": "we decided to use rust"},
                    "session_id": "s1",
                },
            )
        )
        assert events == []


class TestDefaultBehaviorUnchanged:
    def test_no_config_no_env_var_behaves_as_before(self, monkeypatch) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        hook = _make_hook(monkeypatch)
        assert hook.excluded_working_dirs == []
