"""
Pin the amplifier-core ToolResult contract for every PalaceTool operation.

``amplifier_core.ToolResult`` is a pydantic model with ``success`` / ``output``
/ ``error`` fields (core:docs/contracts/TOOL_CONTRACT.md) -- NOT ``content`` /
``is_error``. Because ``BaseModel.__init__(**data)`` silently drops unknown
kwargs, a tool that constructs ``ToolResult(content=..., is_error=True)``
gets the all-defaults ``ToolResult(success=True, output=None, error=None)``
back -- a hard failure silently reported as success with null output.

These tests pin the real contract across every operation branch of
``PalaceTool.execute``: a failing MCP call (or subprocess) must produce
``success=False`` with a non-empty ``error``, and a successful call must
produce ``success=True`` with real (non-null) ``output``.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

import amplifier_module_tool_mempalace as tm
from amplifier_module_tool_mempalace import PalaceTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously (helper for async execute calls)."""
    return asyncio.run(coro)


def _assert_contract_success(result: Any) -> None:
    """A successful ToolResult must have success=True and non-null output."""
    assert result.success is True, f"expected success, got error={result.error}"
    assert result.output is not None, "successful ToolResult must carry real output"
    assert result.error is None


def _assert_contract_failure(result: Any) -> None:
    """A failed ToolResult must have success=False and a non-empty error.

    Never null-output-with-implicit-success -- the exact regression this
    suite guards against.
    """
    assert result.success is False, (
        f"expected failure, got success=True output={result.output!r} "
        "(this is the silent-success regression: content=/is_error= kwargs "
        "are dropped by pydantic, leaving all ToolResult defaults)"
    )
    assert result.error is not None, "failed ToolResult must carry a non-empty error"
    assert result.error.get("message"), "error dict must have a non-empty message"


# ---------------------------------------------------------------------------
# search / remember / status / traverse -- simple pass-through operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "kwargs", "mcp_tool_name"),
    [
        ("search", {"query": "hello"}, "mempalace_search"),
        ("remember", {"content": "verbatim text"}, "mempalace_add_drawer"),
        ("status", {}, "mempalace_status"),
        ("traverse", {"start_room": "r1"}, "mempalace_traverse"),
    ],
)
class TestSimplePassthroughOperations:
    def test_success_returns_success_true_with_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        kwargs: dict,
        mcp_tool_name: str,
    ) -> None:
        monkeypatch.setattr(
            tm, "_mcp_call", lambda name, args: {"ok": True, "tool": name}
        )
        tool = PalaceTool()
        result = _run(tool.execute({"operation": operation, **kwargs}))
        _assert_contract_success(result)
        assert mcp_tool_name in result.output

    def test_mcp_failure_returns_success_false_with_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        kwargs: dict,
        mcp_tool_name: str,
    ) -> None:
        """MCP unavailable (e.g. mempalace-mcp not installed/reachable)."""
        monkeypatch.setattr(
            tm,
            "_mcp_call",
            lambda name, args: {"error": "mempalace-mcp unavailable"},
        )
        tool = PalaceTool()
        result = _run(tool.execute({"operation": operation, **kwargs}))
        _assert_contract_failure(result)
        assert "mempalace-mcp unavailable" in result.error["message"]


# ---------------------------------------------------------------------------
# kg -- query / add / invalidate / timeline / stats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kg_action", ["query", "add", "invalidate", "timeline", "stats"]
)
class TestKgOperation:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, kg_action: str) -> None:
        monkeypatch.setattr(tm, "_mcp_call", lambda name, args: {"facts": []})
        tool = PalaceTool()
        result = _run(
            tool.execute(
                {
                    "operation": "kg",
                    "kg_action": kg_action,
                    "entity": "e",
                    "subject": "s",
                    "predicate": "p",
                    "object": "o",
                }
            )
        )
        _assert_contract_success(result)

    def test_mcp_failure(self, monkeypatch: pytest.MonkeyPatch, kg_action: str) -> None:
        monkeypatch.setattr(
            tm, "_mcp_call", lambda name, args: {"error": "kg store unreachable"}
        )
        tool = PalaceTool()
        result = _run(
            tool.execute(
                {
                    "operation": "kg",
                    "kg_action": kg_action,
                    "entity": "e",
                    "subject": "s",
                    "predicate": "p",
                    "object": "o",
                }
            )
        )
        _assert_contract_failure(result)
        assert "kg store unreachable" in result.error["message"]


# ---------------------------------------------------------------------------
# diary -- read / write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("diary_action", ["read", "write"])
class TestDiaryOperation:
    def test_success(
        self, monkeypatch: pytest.MonkeyPatch, diary_action: str
    ) -> None:
        monkeypatch.setattr(tm, "_mcp_call", lambda name, args: {"entries": []})
        tool = PalaceTool()
        result = _run(
            tool.execute(
                {
                    "operation": "diary",
                    "diary_action": diary_action,
                    "agent_name": "curator",
                    "entry": "an entry",
                }
            )
        )
        _assert_contract_success(result)

    def test_mcp_failure(
        self, monkeypatch: pytest.MonkeyPatch, diary_action: str
    ) -> None:
        monkeypatch.setattr(
            tm, "_mcp_call", lambda name, args: {"error": "diary store unavailable"}
        )
        tool = PalaceTool()
        result = _run(
            tool.execute(
                {
                    "operation": "diary",
                    "diary_action": diary_action,
                    "agent_name": "curator",
                    "entry": "an entry",
                }
            )
        )
        _assert_contract_failure(result)
        assert "diary store unavailable" in result.error["message"]


# ---------------------------------------------------------------------------
# mine -- subprocess-backed operation
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestMineOperation:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, stdout="mined 3 files"),
        )
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "mine", "path": "."}))
        _assert_contract_success(result)
        assert "mined 3 files" in result.output

    def test_nonzero_exit_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing `mempalace mine` subprocess (non-zero exit) must be a
        loud failure, not a silent success with the stderr text as if it
        were normal output."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                1, stderr="mempalace: no palace initialized"
            ),
        )
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "mine", "path": "."}))
        _assert_contract_failure(result)
        assert "no palace initialized" in result.error["message"]

    def test_timeout_expired_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_timeout(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="mempalace mine", timeout=120)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "mine", "path": "."}))
        _assert_contract_failure(result)
        assert "timed out" in result.error["message"].lower()

    def test_cli_not_found_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_fnf(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("mempalace")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "mine", "path": "."}))
        _assert_contract_failure(result)
        assert "mempalace" in result.error["message"].lower()

    def test_unexpected_exception_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(subprocess, "run", raise_boom)
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "mine", "path": "."}))
        _assert_contract_failure(result)
        assert "boom" in result.error["message"]


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------


def test_unknown_operation_returns_success_false_with_error() -> None:
    tool = PalaceTool()
    result = _run(tool.execute({"operation": "not_a_real_operation"}))
    _assert_contract_failure(result)
    assert "not_a_real_operation" in result.error["message"]


# ---------------------------------------------------------------------------
# Orchestrator calling convention (protocol shape) -- the regression this
# module shipped in production: `Tool.execute` MUST accept a single
# positional dict, matching amplifier_core.interfaces.Tool.execute(self,
# input: dict[str, Any]). The real loop orchestrator calls
# `await tool.execute(tool_call.arguments)` -- a raw dict, positional,
# never kwargs-expanded. A signature of
# `execute(self, operation: str, **kwargs)` silently binds the ENTIRE
# arguments dict to `operation`, so every real call falls through to the
# "Unknown operation" branch. Tests that call `execute(operation=..., **kw)`
# never catch this because they bypass the orchestrator's calling
# convention entirely.
# ---------------------------------------------------------------------------


class TestOrchestratorCallingConvention:
    def test_execute_matches_orchestrator_calling_convention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the exact convention loop orchestrators use:
        tool.execute(tool_call.arguments) -- one positional dict, no kwargs.
        """
        monkeypatch.setattr(
            tm, "_mcp_call", lambda name, args: {"ok": True, "tool": name}
        )
        tool = PalaceTool()
        result = _run(tool.execute({"operation": "status"}))
        _assert_contract_success(result)
        assert "mempalace_status" in result.output


# ---------------------------------------------------------------------------
# Missing operation key -- must fail loudly, never crash
# ---------------------------------------------------------------------------


class TestMissingOperationEdgeCase:
    def test_execute_with_empty_dict_returns_failure_not_crash(self) -> None:
        """execute({}) -- no `operation` key at all -- must return
        success=False with a clear error message, never raise."""
        tool = PalaceTool()
        result = _run(tool.execute({}))
        _assert_contract_failure(result)
        assert "Unknown operation" in result.error["message"]

