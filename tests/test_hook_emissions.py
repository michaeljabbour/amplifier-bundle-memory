"""
Bundle-level integration tests for hook emission wiring (Section 8.6).

These tests verify that each hook correctly calls emit_event with the right
hook name and event name. emit_event itself is patched so tests don't touch
the filesystem, keeping them fast and isolated.

Each hook's invocation matches amplifier-core's current handler contract:
    async def __call__(event: str, data: dict) -> HookResult

Capture-hook latency model
--------------------------
The capture hook intentionally does no slow work in __call__.  It enqueues
a job and returns; a daemon drain thread does the slow work (git wing
detection, mempalace mcp drawer write).  Tests that need to assert the
drained behaviour call ``m._QUEUE.join()`` to wait for the worker.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Capture hook
# ---------------------------------------------------------------------------


def _patch_capture_emitter(
    monkeypatch: pytest.MonkeyPatch, emitted: list[tuple[Any, ...]]
) -> None:
    """Patch emit_event in BOTH the hook module and the drain thread's view.

    The drain thread imports the symbol once at module load.  Re-binding the
    module attribute is sufficient because both the sync handler and the
    drain worker reference it by name (``emit_event``) at call time.
    """
    import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

    lock = threading.Lock()

    def _capture(*a: Any, **kw: Any) -> None:
        with lock:
            emitted.append((a, kw))

    monkeypatch.setattr(m, "emit_event", _capture)


def _drain(timeout: float = 5.0) -> None:
    """Wait for the capture queue to fully drain. Test-only helper."""
    import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

    if m._QUEUE is None:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if m._QUEUE.unfinished_tasks == 0:
            return
        time.sleep(0.01)
    raise AssertionError("capture queue did not drain within timeout")


@pytest.fixture(autouse=True)
def _drain_capture_queue_between_tests() -> Any:
    """Ensure each capture-hook test starts and ends with an empty queue.

    The drain thread is a module-level singleton — without this fixture, a
    job left in flight by one test would emit drawer_filed into the next
    test's monkeypatched event list and confuse assertions.
    """
    yield
    try:
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        if m._QUEUE is not None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and m._QUEUE.unfinished_tasks > 0:
                time.sleep(0.01)
    except Exception:
        pass


class TestCaptureHookEmissions:
    def test_capture_emits_queued_synchronously(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """tool:post with worthy content → capture_queued is emitted by the
        time __call__ returns. drawer_filed arrives only after the drain."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        _patch_capture_emitter(monkeypatch, emitted)
        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        # Redirect the spool dir into tmp_path so we don't touch ~/.mempalace
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        hook = m.MempalaceCaptureHook()
        asyncio.run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {"command": "ls -la"},
                    "tool_output": "x" * 200,
                },
            )
        )

        # Synchronous contract: capture_queued is recorded before __call__ returns.
        queued = [e for e in emitted if e[0][1] == "capture_queued"]
        assert len(queued) == 1, f"Expected capture_queued in {emitted}"
        _args, kwargs = queued[0]
        assert _args[0] == "mempalace-capture"
        assert kwargs.get("ok") is True
        assert "capture_id" in kwargs.get("data", {})

        # Now wait for the worker. drawer_filed should arrive after drain.
        _drain()
        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1, f"Expected drawer_filed after drain in {emitted}"
        assert (
            filed[0][1].get("data", {}).get("capture_id")
            == kwargs["data"]["capture_id"]
        )
        assert "wing" in filed[0][1].get("data", {})

    def test_capture_returns_fast_under_slow_drawer_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """The hot-path __call__ must not block on the slow mempalace subprocess.

        Simulate a 2-second drawer write and assert __call__ completes well
        under that.  This is the explicit latency contract.
        """
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        _patch_capture_emitter(monkeypatch, emitted)

        # Worker functions sleep 0.2s each — long enough that the contract
        # would fail if __call__ blocked on them, short enough that the
        # autouse drain fixture can clean up before the next test.
        def slow_write(*a: Any, **kw: Any) -> None:
            time.sleep(0.2)

        monkeypatch.setattr(m, "_mcp_add_drawer", slow_write)
        monkeypatch.setattr(m, "_detect_wing", lambda: (slow_write(), "wing_test")[1])
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        hook = m.MempalaceCaptureHook()

        start = time.monotonic()
        asyncio.run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": "x" * 200,
                },
            )
        )
        elapsed = time.monotonic() - start

        # The handler must return fast even though downstream work takes ~0.4s.
        # 0.05s is a comfortable bound — typical local times are sub-millisecond.
        assert elapsed < 0.05, (
            f"__call__ took {elapsed:.3f}s — the slow drawer write must NOT "
            f"block the kernel handler."
        )
        assert any(e[0][1] == "capture_queued" for e in emitted)

    def test_capture_emits_on_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool:post with too-short output → capture_skipped event (sync path)."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        _patch_capture_emitter(monkeypatch, emitted)

        hook = m.MempalaceCaptureHook()
        asyncio.run(
            hook(
                "tool:post",
                {"tool_name": "bash", "tool_input": {}, "tool_output": "short"},
            )
        )

        skipped = [e for e in emitted if e[0][1] == "capture_skipped"]
        assert len(skipped) == 1, f"Expected capture_skipped in {emitted}"
        _args, kwargs = skipped[0]
        assert _args[0] == "mempalace-capture"
        assert kwargs.get("ok") is False
        assert kwargs["data"]["reason"] == "too_short"

    def test_capture_no_emit_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """emit_events=False → no calls to emit_event at all (sync or drained)."""
        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        _patch_capture_emitter(monkeypatch, emitted)
        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        monkeypatch.setattr(
            m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
        )

        hook = m.MempalaceCaptureHook(config={"emit_events": False})
        asyncio.run(
            hook(
                "tool:post",
                {"tool_name": "bash", "tool_input": {}, "tool_output": "x" * 200},
            )
        )
        _drain()
        assert emitted == []

    def test_replay_orphans_re_enqueues_unfinished(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """An orphan spool entry (queued without completion) is re-enqueued
        and drained when _replay_orphans is called.  Models session re-hydration.
        """
        import json

        import amplifier_module_hooks_mempalace_capture as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        _patch_capture_emitter(monkeypatch, emitted)
        monkeypatch.setattr(m, "_mcp_add_drawer", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
        # Pretend mempalace is initialised: spool dir lives under tmp_path.
        spool_dir = tmp_path / "spool" / "test_session"
        spool_dir.mkdir(parents=True)
        monkeypatch.setattr(m, "_spool_dir_for", lambda sid: spool_dir)
        # And no completion events exist for this session.
        monkeypatch.setattr(m, "read_events", lambda *a, **kw: [])

        # Plant an orphan.
        capture_id = "orphan-xyz"
        payload = {
            "capture_id": capture_id,
            "tool_name": "bash",
            "tool_input": {},
            "tool_output": "x" * 200,
            "source": "bash",
            "category": None,
            "session_id": "test_session",
            "enqueued_at": "2026-04-29T00:00:00+00:00",
            "auto_wing": True,
            "auto_room": True,
            "config_wing": "wing_general",
            "config_room": "general",
            "emit_events": True,
        }
        (spool_dir / f"{capture_id}.json").write_text(json.dumps(payload))

        replayed = m._replay_orphans("test_session", emit_events=True)
        assert replayed == 1
        _drain()

        filed = [
            e
            for e in emitted
            if e[0][1] == "drawer_filed"
            and e[1].get("data", {}).get("capture_id") == capture_id
        ]
        assert len(filed) == 1, f"Expected replay to produce drawer_filed in {emitted}"
        # And the spool file should be gone.
        assert not (spool_dir / f"{capture_id}.json").exists()


# ---------------------------------------------------------------------------
# Briefing hook
# ---------------------------------------------------------------------------


class TestBriefingHookEmissions:
    def test_briefing_emits_on_assemble(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """session:start with results → briefing_assembled event emitted."""
        import amplifier_module_hooks_mempalace_briefing as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        def fake_run(cmd: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(m.subprocess, "run", fake_run)
        monkeypatch.setattr(
            m,
            "_build_briefing",
            lambda **kw: ("## Briefing\ntest content", ["semantic"], 100, [], []),
        )
        monkeypatch.setattr(m, "_detect_project_name", lambda: "testproject")

        hook = m.MempalaceBriefingHook()
        result = asyncio.run(hook("session:start", {"opening_prompt": "start working"}))

        assert result.action == "inject_context"
        assembled = [e for e in emitted if e[0][1] == "briefing_assembled"]
        assert len(assembled) == 1, f"Expected briefing_assembled in {emitted}"
        _args, kwargs = assembled[0]
        assert _args[0] == "mempalace-briefing"
        assert kwargs.get("ok") is True
        data = kwargs.get("data", {})
        assert "project" in data
        assert "section_count" in data
        assert len(data["results_fetched"]) == len(data["results_after_rerank"])
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
        asyncio.run(hook("session:start", {}))

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
        asyncio.run(hook("session:start", {}))
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
            {
                "id": "mem_1",
                "text": "test memory content",
                "score": 0.85,
                "metadata": {},
            }
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

        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        (pc_dir / "HANDOFF.md").write_text(
            "# Handoff\n\nSome content here.", encoding="utf-8"
        )

        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        hook = m.ProjectContextStartHook()
        result = asyncio.run(hook("session:start", {}))

        assert result.action == "inject_context"
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
        asyncio.run(hook("session:start", {}))

        scaffolded = [e for e in emitted if e[0][1] == "coordination_scaffolded"]
        assert len(scaffolded) == 1, f"Expected coordination_scaffolded in {emitted}"
        data = scaffolded[0][1]["data"]
        assert "files_created" in data
        assert len(data["files_created"]) > 0

    def test_project_context_emits_curator_handoff_requested(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session:end → curator_handoff_requested event."""
        import amplifier_module_hooks_project_context as m  # type: ignore[import]

        emitted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: emitted.append((a, kw)))

        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc_dir)

        hook = m.ProjectContextEndHook()
        asyncio.run(hook("session:end", {}))

        requested = [e for e in emitted if e[0][1] == "curator_handoff_requested"]
        assert len(requested) == 1, f"Expected curator_handoff_requested in {emitted}"
        data = requested[0][1]["data"]
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
        asyncio.run(hook("session:start", {}))
        assert emitted == []
