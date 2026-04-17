"""
Bundle-level integration tests for hook emission wiring (Section 8.6).

These tests verify that each hook correctly calls emit_event with the right
hook name and event name. emit_event itself is patched so tests don't touch
the filesystem, keeping them fast and isolated.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeHookContext:
    """Minimal stub for HookContext used by capture, briefing, project-context hooks."""

    def __init__(
        self, event: dict[str, Any] | None = None, session_id: str | None = None
    ) -> None:
        self.event: dict[str, Any] = event or {}
        self.session_id = session_id
        self.injected: list[str] = []
        self.delegations: list[dict[str, Any]] = []

    def inject_context(self, content: str, *, ephemeral: bool = True) -> None:
        self.injected.append(content)

    def delegate_to_agent(self, agent: str, *, prompt: str) -> None:
        self.delegations.append({"agent": agent, "prompt": prompt})


# ---------------------------------------------------------------------------
# Capture hook
# ---------------------------------------------------------------------------


class TestCaptureHookEmissions:
    def test_capture_emits_on_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool:post with worthy content → drawer_filed event emitted."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))
        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")

        hook = m.MempalaceCaptureHook()
        ctx = FakeHookContext(
            event={
                "tool_name": "bash",
                "tool_input": {"command": "ls -la"},
                "tool_output": "x" * 200,  # worthy: >50 chars, <8192 chars
            }
        )
        asyncio.run(hook.handle(ctx))

        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1, f"Expected drawer_filed in {emitted}"
        _args, kwargs = filed[0]
        assert _args[0] == "mempalace-capture"
        assert kwargs.get("ok") is True
        assert "wing" in kwargs.get("data", {})

    def test_capture_emits_on_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool:post with too-short output → capture_skipped event."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceCaptureHook()
        ctx = FakeHookContext(
            event={
                "tool_name": "bash",
                "tool_input": {},
                "tool_output": "short",  # < 50 chars → too_short
            }
        )
        asyncio.run(hook.handle(ctx))

        skipped = [e for e in emitted if e[0][1] == "capture_skipped"]
        assert len(skipped) == 1, f"Expected capture_skipped in {emitted}"
        _args, kwargs = skipped[0]
        assert _args[0] == "mempalace-capture"
        assert kwargs.get("ok") is False
        assert kwargs["data"]["reason"] == "too_short"

    def test_capture_no_emit_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """emit_events=False → no calls to emit_event at all."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceCaptureHook(config={"emit_events": False})
        ctx = FakeHookContext(
            event={"tool_name": "bash", "tool_input": {}, "tool_output": "short"}
        )
        asyncio.run(hook.handle(ctx))
        assert emitted == []


# ---------------------------------------------------------------------------
# Briefing hook
# ---------------------------------------------------------------------------


class TestBriefingHookEmissions:
    def test_briefing_emits_on_assemble(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """session:start with results → briefing_assembled event emitted."""
        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        # Mock subprocess.run so mempalace appears available
        def fake_run(
            cmd: Any, *a: Any, **kw: Any
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(m.subprocess, "run", fake_run)

        # Mock _build_briefing to return a non-empty briefing
        monkeypatch.setattr(
            m,
            "_build_briefing",
            lambda **kw: ("## Briefing\ntest content", ["semantic"], 100, 3, 3),
        )
        monkeypatch.setattr(m, "_detect_project_name", lambda: "testproject")

        hook = m.MempalaceBriefingHook()
        ctx = FakeHookContext(event={"opening_prompt": "start working"})
        asyncio.run(hook.handle(ctx))

        assembled = [e for e in emitted if e[0][1] == "briefing_assembled"]
        assert len(assembled) == 1, f"Expected briefing_assembled in {emitted}"
        _args, kwargs = assembled[0]
        assert _args[0] == "mempalace-briefing"
        assert kwargs.get("ok") is True
        data = kwargs.get("data", {})
        assert "project" in data
        assert "section_count" in data
        assert data["results_fetched"] == data["results_after_rerank"]
        assert data["importance_weight"] == 1.0

    def test_briefing_emits_skip_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When mempalace CLI is not found → briefing_skipped with mempalace_unavailable."""
        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        def raise_not_found(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError("mempalace not found")

        monkeypatch.setattr(m.subprocess, "run", raise_not_found)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        hook = m.MempalaceBriefingHook()
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))

        skipped = [e for e in emitted if e[0][1] == "briefing_skipped"]
        assert len(skipped) == 1
        assert skipped[0][1]["data"]["reason"] == "mempalace_unavailable"

    def test_briefing_no_emit_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """emit_events=False → no calls to emit_event."""
        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        def raise_not_found(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(m.subprocess, "run", raise_not_found)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        hook = m.MempalaceBriefingHook(config={"emit_events": False})
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))
        assert emitted == []


# ---------------------------------------------------------------------------
# Interject hook
# ---------------------------------------------------------------------------


class TestInterjectHookEmissions:
    @pytest.mark.asyncio
    async def test_interject_emits_on_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt:submit with matching memory → memory_surfaced event."""
        import amplifier_module_hooks_mempalace_interject as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceInterjectHook({})
        memories = [
            {"id": "mem_1", "text": "test memory content", "score": 0.85, "metadata": {}}
        ]

        hook._retrieve_and_gate = AsyncMock(  # type: ignore[method-assign]
            return_value=(memories, True, "", False)
        )

        await hook.on_prompt_submit(
            "prompt:submit",
            {
                "prompt": "this is a long enough prompt to pass the length check",
                "session_id": "test123",
            },
        )

        surfaced = [e for e in emitted if e[0][1] == "memory_surfaced"]
        assert len(surfaced) == 1, f"Expected memory_surfaced in {emitted}"
        _args, kwargs = surfaced[0]
        assert _args[0] == "mempalace-interject"
        assert kwargs.get("ok") is True
        data = kwargs.get("data", {})
        assert data["trigger"] == "prompt_submit"
        assert data["memory_ids"] == ["mem_1"]
        assert data["top_score"] == 0.85

    @pytest.mark.asyncio
    async def test_interject_emits_skip_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt_enabled=False → interject_skipped with reason=disabled."""
        import amplifier_module_hooks_mempalace_interject as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceInterjectHook({"prompt_enabled": False})
        await hook.on_prompt_submit(
            "prompt:submit", {"prompt": "a long enough prompt here", "session_id": "t"}
        )

        skipped = [e for e in emitted if e[0][1] == "interject_skipped"]
        assert len(skipped) == 1
        assert skipped[0][1]["data"]["reason"] == "disabled"
        assert skipped[0][1]["data"]["trigger"] == "prompt_submit"

    @pytest.mark.asyncio
    async def test_interject_emits_skip_too_short(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Short prompt → interject_skipped with reason=too_short."""
        import amplifier_module_hooks_mempalace_interject as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceInterjectHook({})
        await hook.on_prompt_submit("prompt:submit", {"prompt": "hi"})

        skipped = [e for e in emitted if e[0][1] == "interject_skipped"]
        assert len(skipped) == 1
        assert skipped[0][1]["data"]["reason"] == "too_short"

    @pytest.mark.asyncio
    async def test_interject_no_emit_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """emit_events=False → no emit calls."""
        import amplifier_module_hooks_mempalace_interject as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        hook = m.MempalaceInterjectHook({"emit_events": False, "prompt_enabled": False})
        await hook.on_prompt_submit("prompt:submit", {"prompt": "hi"})
        assert emitted == []


# ---------------------------------------------------------------------------
# Project-context hook
# ---------------------------------------------------------------------------


class TestProjectContextHookEmissions:
    def test_project_context_emits_on_read(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session:start with project-context/ → coordination_read event."""
        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        # Create a project-context/ dir with a HANDOFF.md
        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        (pc_dir / "HANDOFF.md").write_text(
            "# Handoff\n\nSome content here.", encoding="utf-8"
        )

        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        hook = m.ProjectContextStartHook()
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))

        read_events = [e for e in emitted if e[0][1] == "coordination_read"]
        assert len(read_events) == 1, f"Expected coordination_read in {emitted}"
        _args, kwargs = read_events[0]
        assert _args[0] == "project-context"
        assert kwargs.get("ok") is True
        data = kwargs.get("data", {})
        assert "files_read" in data
        assert len(data["files_read"]) >= 1
        assert "token_estimate" in data

    def test_project_context_emits_on_scaffold(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session:start with no project-context/ → coordination_scaffolded event."""
        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)
        monkeypatch.setattr(m, "_find_git_root", lambda: tmp_path)

        hook = m.ProjectContextStartHook(config={"tier1_always": False})
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))

        scaffolded = [e for e in emitted if e[0][1] == "coordination_scaffolded"]
        assert len(scaffolded) == 1, f"Expected coordination_scaffolded in {emitted}"
        data = scaffolded[0][1]["data"]
        assert "files_created" in data
        assert len(data["files_created"]) > 0

    def test_project_context_emits_curator_delegated(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session:end → curator_delegated event."""
        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        hook = m.ProjectContextEndHook()
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))

        delegated = [e for e in emitted if e[0][1] == "curator_delegated"]
        assert len(delegated) == 1, f"Expected curator_delegated in {emitted}"
        data = delegated[0][1]["data"]
        assert "prompt_preview" in data
        assert len(data["prompt_preview"]) > 0

    def test_project_context_no_emit_when_disabled(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """emit_events=False → no emit calls."""
        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        (pc_dir / "HANDOFF.md").write_text("content", encoding="utf-8")
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        hook = m.ProjectContextStartHook(config={"emit_events": False})
        ctx = FakeHookContext()
        asyncio.run(hook.handle(ctx))
        assert emitted == []
