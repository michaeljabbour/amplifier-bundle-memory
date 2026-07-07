"""
Pin the amplifier-core ToolResult contract for every MemoryTool operation.

``amplifier_core.ToolResult`` is a pydantic model with ``success`` / ``output``
/ ``error`` fields (core:docs/contracts/TOOL_CONTRACT.md) -- NOT ``content`` /
``is_error``. Because ``BaseModel.__init__(**data)`` silently drops unknown
kwargs, a tool that constructs ``ToolResult(content=..., is_error=True)``
gets the all-defaults ``ToolResult(success=True, output=None, error=None)``
back -- a hard failure silently reported as success with null output.

These tests pin the real contract across every operation branch of
``MemoryTool.execute``: a failing call (or subprocess) must produce
``success=False`` with a non-empty ``error``, and a successful call must
produce ``success=True`` with real (non-null) ``output``.

Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): every
operation now routes through ``_call_client`` (MemoryClient via
``ensure_daemon()``) instead of a vendor subprocess. Tests
patch ``tm._call_client`` directly -- the ONE transport seam -- for both the
success AND failure branches (failure now means "memory daemon unavailable",
not a vendor-specific error string). The one-positional-dict orchestrator
calling convention pin (``execute(input: dict)``) is UNCHANGED.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_tool_memory as tm
import pytest
from amplifier_module_tool_memory import MemoryTool

_DAEMON_UNAVAILABLE = "memory daemon unavailable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously (helper for async execute calls)."""
    return asyncio.run(coro)


def _raise_daemon_unavailable(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError(_DAEMON_UNAVAILABLE)


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
    ("operation", "kwargs", "client_method"),
    [
        ("search", {"query": "hello"}, "search"),
        ("remember", {"content": "verbatim text"}, "remember"),
        ("status", {}, "status"),
        ("traverse", {"start_room": "r1"}, "traverse"),
    ],
)
class TestSimplePassthroughOperations:
    def test_success_returns_success_true_with_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        kwargs: dict,
        client_method: str,
    ) -> None:
        monkeypatch.setattr(
            tm, "_call_client", lambda method, **kw: {"ok": True, "method": method}
        )
        tool = MemoryTool()
        result = _run(tool.execute({"operation": operation, **kwargs}))
        _assert_contract_success(result)
        assert client_method in result.output

    def test_daemon_unavailable_returns_success_false_with_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        kwargs: dict,
        client_method: str,
    ) -> None:
        """Memory daemon unavailable (auto-start failed / unreachable)."""
        monkeypatch.setattr(tm, "_call_client", _raise_daemon_unavailable)
        tool = MemoryTool()
        result = _run(tool.execute({"operation": operation, **kwargs}))
        _assert_contract_failure(result)
        assert _DAEMON_UNAVAILABLE in result.error["message"]


# ---------------------------------------------------------------------------
# kg -- query / add / invalidate / timeline / stats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kg_action", ["query", "add", "invalidate", "timeline", "stats"]
)
class TestKgOperation:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, kg_action: str) -> None:
        monkeypatch.setattr(tm, "_call_client", lambda method, **kw: [])
        tool = MemoryTool()
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

    def test_daemon_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, kg_action: str
    ) -> None:
        monkeypatch.setattr(tm, "_call_client", _raise_daemon_unavailable)
        tool = MemoryTool()
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
        assert _DAEMON_UNAVAILABLE in result.error["message"]


# ---------------------------------------------------------------------------
# diary -- read / write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("diary_action", ["read", "write"])
class TestDiaryOperation:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, diary_action: str) -> None:
        monkeypatch.setattr(tm, "_call_client", lambda method, **kw: [])
        tool = MemoryTool()
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

    def test_daemon_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, diary_action: str
    ) -> None:
        monkeypatch.setattr(tm, "_call_client", _raise_daemon_unavailable)
        tool = MemoryTool()
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
        assert _DAEMON_UNAVAILABLE in result.error["message"]


# ---------------------------------------------------------------------------
# mine -- native file-walker + conversation-parser operation (no subprocess)
# ---------------------------------------------------------------------------


class TestMineOperation:
    def test_success_files_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        (tmp_path / "note.md").write_text(
            "# Title\n\nSome useful project notes worth remembering.\n",
            encoding="utf-8",
        )
        remembered: list[dict] = []
        monkeypatch.setattr(
            tm,
            "_call_client",
            lambda method, **kw: remembered.append(kw) or "ref-1",
        )
        tool = MemoryTool()
        result = _run(tool.execute({"operation": "mine", "path": str(tmp_path)}))
        _assert_contract_success(result)
        assert "files_scanned" in result.output
        assert len(remembered) >= 1

    def test_convos_mode_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        convo_file = tmp_path / "transcript.jsonl"
        convo_file.write_text(
            '{"role": "user", "content": "hello there, this is a real message"}\n'
            '{"role": "assistant", "content": "hi! here is a helpful reply"}\n',
            encoding="utf-8",
        )
        remembered: list[dict] = []
        monkeypatch.setattr(
            tm,
            "_call_client",
            lambda method, **kw: remembered.append(kw) or "ref-1",
        )
        tool = MemoryTool()
        result = _run(
            tool.execute(
                {"operation": "mine", "path": str(convo_file), "mode": "convos"}
            )
        )
        _assert_contract_success(result)
        assert "messages_filed" in result.output
        assert len(remembered) == 2

    def test_missing_path_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path that doesn't exist must be a loud failure, never a silent
        success with an empty summary."""
        tool = MemoryTool()
        result = _run(
            tool.execute({"operation": "mine", "path": "/no/such/path/at/all"})
        )
        _assert_contract_failure(result)
        assert "does not exist" in result.error["message"]

    def test_daemon_unavailable_during_remember_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        (tmp_path / "note.md").write_text(
            "Some content that will be chunked and filed.\n", encoding="utf-8"
        )
        monkeypatch.setattr(tm, "_call_client", _raise_daemon_unavailable)
        tool = MemoryTool()
        result = _run(tool.execute({"operation": "mine", "path": str(tmp_path)}))
        _assert_contract_failure(result)
        assert _DAEMON_UNAVAILABLE in result.error["message"]

    def test_unexpected_exception_returns_success_false_with_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        (tmp_path / "note.md").write_text("content\n", encoding="utf-8")

        def raise_boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(tm, "_call_client", raise_boom)
        tool = MemoryTool()
        result = _run(tool.execute({"operation": "mine", "path": str(tmp_path)}))
        _assert_contract_failure(result)
        assert "boom" in result.error["message"]


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------


def test_unknown_operation_returns_success_false_with_error() -> None:
    tool = MemoryTool()
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
            tm, "_call_client", lambda method, **kw: {"ok": True, "method": method}
        )
        tool = MemoryTool()
        result = _run(tool.execute({"operation": "status"}))
        _assert_contract_success(result)
        assert "status" in result.output


# ---------------------------------------------------------------------------
# Missing operation key -- must fail loudly, never crash
# ---------------------------------------------------------------------------


class TestMissingOperationEdgeCase:
    def test_execute_with_empty_dict_returns_failure_not_crash(self) -> None:
        """execute({}) -- no `operation` key at all -- must return
        success=False with a clear error message, never raise."""
        tool = MemoryTool()
        result = _run(tool.execute({}))
        _assert_contract_failure(result)
        assert "Unknown operation" in result.error["message"]
