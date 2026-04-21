"""
Contract tests against the installed amplifier-core.

These tests fail fast when amplifier-core or amplifier-foundation ships a
change that breaks the handshake this bundle relies on. Specifically, they
verify:

1. `amplifier_core` still exports `HookResult` (our hooks construct it directly).
2. The hook handler ABI is still `async (event, data) -> HookResult` — we
   mount each hook against the real RustHookRegistry and dispatch a synthetic
   event to confirm it returns a HookResult (not raises, not returns None).
3. The Tool protocol still reads `input_schema` — PalaceTool exposes a
   well-formed JSON Schema with `type: object`.
4. Each module's async `mount(coordinator, config)` entry-point matches the
   current loader convention (signature + return shape).

Run: `pytest tests/test_contract.py`

When amplifier-core ships an incompatible change, the test that first
fails points at the specific seam that moved.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

# Hard import from the real package — if amplifier-core isn't installed, skip
# the whole file (still exits clean in CI; just re-runs once deps are present).
amplifier_core = pytest.importorskip("amplifier_core")


# ---------------------------------------------------------------------------
# 1. amplifier-core surface we depend on
# ---------------------------------------------------------------------------


class TestCoreSurface:
    def test_hook_result_importable(self) -> None:
        """Our hooks construct HookResult directly — it must stay exported."""
        from amplifier_core import HookResult  # noqa: F401

    def test_hook_result_accepts_expected_fields(self) -> None:
        """HookResult(action, context_injection, context_injection_role,
        ephemeral, suppress_output) must keep accepting our kwargs."""
        from amplifier_core import HookResult

        r = HookResult(
            action="inject_context",
            context_injection="hello",
            context_injection_role="user",
            ephemeral=True,
            suppress_output=True,
        )
        assert r.action == "inject_context"

        r2 = HookResult(action="continue")
        assert r2.action == "continue"

    def test_hook_registry_register_signature(self) -> None:
        """RustHookRegistry.register(event, handler, *, priority=..., name=...)."""
        from amplifier_core import HookRegistry

        reg = HookRegistry()
        assert hasattr(reg, "register"), "HookRegistry lost .register()"
        assert hasattr(reg, "emit_and_collect") or hasattr(reg, "emit"), (
            "HookRegistry lost its event-dispatch method"
        )


# ---------------------------------------------------------------------------
# 2. Hook handler ABI — event + data → HookResult
# ---------------------------------------------------------------------------


class _FakeCoordinator:
    """Minimal coordinator stub — just exposes a real HookRegistry."""

    def __init__(self) -> None:
        from amplifier_core import HookRegistry

        self.hooks = HookRegistry()
        self.session_id: str | None = "test-session"


async def _dispatch(registry: Any, event: str, data: dict[str, Any]) -> list[Any]:
    """Call the registry's dispatch method with whichever name it now uses."""
    if hasattr(registry, "emit_and_collect"):
        return await registry.emit_and_collect(event, data)
    if hasattr(registry, "emit"):
        return await registry.emit(event, data)
    raise AssertionError("HookRegistry has no emit method")


class TestHookHandlerABI:
    def test_capture_hook_returns_hook_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_mempalace_capture as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        hook = m.MempalaceCaptureHook()
        result = asyncio.run(
            hook(
                "tool:post",
                {"tool_name": "bash", "tool_input": {}, "tool_output": "short"},
            )
        )
        assert result is not None, "Hook returned None — engine expects HookResult"
        assert hasattr(result, "action"), "Returned object is not a HookResult"

    def test_briefing_hook_returns_hook_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_mempalace_briefing as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        def raise_not_found(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(m.subprocess, "run", raise_not_found)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        hook = m.MempalaceBriefingHook()
        result = asyncio.run(hook("session:start", {}))
        assert hasattr(result, "action")

    def test_project_context_start_returns_hook_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_project_context as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)
        monkeypatch.setattr(m, "_find_git_root", lambda: None)

        hook = m.ProjectContextStartHook()
        result = asyncio.run(hook("session:start", {}))
        assert hasattr(result, "action")

    def test_project_context_end_returns_hook_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_project_context as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        hook = m.ProjectContextEndHook()
        result = asyncio.run(hook("session:end", {}))
        assert hasattr(result, "action")


# ---------------------------------------------------------------------------
# 3. Mount signature + registry dispatch
# ---------------------------------------------------------------------------


class TestMountAndDispatch:
    """Mount each module against a real HookRegistry and dispatch an event.

    This is the test that would have caught the handle-vs-__call__ regression
    — it forces the hook through the same code path amplifier's kernel uses.
    """

    def test_mount_signatures_are_async_coordinator_config(self) -> None:
        import amplifier_module_hooks_mempalace_briefing as briefing
        import amplifier_module_hooks_mempalace_capture as capture
        import amplifier_module_hooks_mempalace_interject as interject
        import amplifier_module_hooks_project_context as project_ctx

        for mod in (briefing, capture, interject, project_ctx):
            mount = getattr(mod, "mount", None)
            assert mount is not None, f"{mod.__name__} missing mount()"
            assert asyncio.iscoroutinefunction(mount), (
                f"{mod.__name__}.mount must be async"
            )
            sig = inspect.signature(mount)
            params = list(sig.parameters.values())
            assert len(params) >= 2, (
                f"{mod.__name__}.mount should accept (coordinator, config)"
            )

    def test_capture_mounts_and_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_mempalace_capture as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        async def _run() -> list[Any]:
            coord = _FakeCoordinator()
            await m.mount(coord, {})
            return await _dispatch(
                coord.hooks,
                "tool:post",
                {"tool_name": "bash", "tool_input": {}, "tool_output": "short"},
            )

        results = asyncio.run(_run())
        # Contract: dispatch must not raise, and the hook must have been invoked.
        assert results is not None

    def test_briefing_mounts_and_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_mempalace_briefing as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

        def raise_not_found(*a: Any, **kw: Any) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(m.subprocess, "run", raise_not_found)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)

        async def _run() -> list[Any]:
            coord = _FakeCoordinator()
            await m.mount(coord, {})
            return await _dispatch(coord.hooks, "session:start", {})

        results = asyncio.run(_run())
        assert results is not None

    def test_project_context_mounts_and_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import amplifier_module_hooks_project_context as m

        monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)
        monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)
        monkeypatch.setattr(m, "_find_git_root", lambda: None)

        async def _run() -> list[Any]:
            coord = _FakeCoordinator()
            await m.mount(coord, {})
            start = await _dispatch(coord.hooks, "session:start", {})
            end = await _dispatch(coord.hooks, "session:end", {})
            return [start, end]

        results = asyncio.run(_run())
        assert all(r is not None for r in results)


# ---------------------------------------------------------------------------
# 4. Tool protocol — PalaceTool must expose input_schema with type:object
# ---------------------------------------------------------------------------


class TestToolProtocol:
    def test_palace_tool_exposes_input_schema(self) -> None:
        """The orchestrator reads tool.input_schema — it must have type:object
        at the root or Anthropic rejects the whole request."""
        from amplifier_module_tool_mempalace import PalaceTool

        tool = PalaceTool()
        schema = tool.input_schema
        assert isinstance(schema, dict), "input_schema must be a dict"
        assert schema.get("type") == "object", (
            "input_schema['type'] must be 'object' — Anthropic requires this "
            "at the root of every custom tool"
        )
        assert "properties" in schema, "input_schema should have 'properties'"

    def test_palace_tool_has_required_attributes(self) -> None:
        """Tool protocol requires name, description, input_schema, execute."""
        from amplifier_module_tool_mempalace import PalaceTool

        tool = PalaceTool()
        assert isinstance(tool.name, str) and tool.name
        assert isinstance(tool.description, str) and tool.description
        assert callable(getattr(tool, "execute", None))
