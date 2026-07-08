"""Contract tests: hooks-memory-capture against the REAL orchestrator payload.

Root cause (DTU-validated, live-session capture never fired): the capture
hook's ``__call__`` read ``data["tool_output"]`` / ``data["is_error"]`` /
``data["success"]`` -- a flat shape no real orchestrator ever sends. The
actual contract, verified against amplifier-module-loop-streaming's
``tool:post`` emission (both the sequential and parallel tool-execution
paths), is:

    {
        "tool_name": str,
        "tool_call_id": str,
        "tool_input": dict,
        "result": <ToolResult.model_dump()>,   # {"success", "output", "error"}
        "parallel_group_id": str | None,
    }

These tests feed the hook the VERBATIM shape above and assert a capture is
enqueued (not gated as ``too_short``), and that the outcome signal
(``tool_success``) is read correctly from the nested ``result`` dict for
both the success and failure cases.

Contrast with ``tests/test_hook_emissions.py`` and
``modules/hooks-memory-capture/tests/test_capture_skipped_bridge.py``, which
exercise the flat ``{"tool_output": ..., "is_error": ..., "success": ...}``
shape -- that is the hook's *legacy/direct-caller fallback*, not the real
orchestrator contract, and is intentionally kept as fallback-path coverage.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _drain_capture_queue_between_tests() -> Any:
    """Ensure each test starts and ends with an empty drain queue.

    The drain thread is a module-level singleton -- without this, a job left
    in flight by one test could bleed into the next test's assertions.
    """
    import time

    yield
    import amplifier_module_hooks_memory_capture as m

    if m._QUEUE is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and m._QUEUE.unfinished_tasks > 0:
            time.sleep(0.01)


def _make_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **config: Any):
    """Build a hook wired to record emit_event calls without touching disk."""
    import amplifier_module_hooks_memory_capture as m

    emitted: list[tuple[Any, ...]] = []

    def _capture(*a: Any, **kw: Any) -> None:
        emitted.append((a, kw))

    monkeypatch.setattr(m, "emit_event", _capture)
    monkeypatch.setattr(m, "_file_drawer", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
    monkeypatch.setattr(
        m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
    )

    hook = m.MemoryCaptureHook(config=config)
    return hook, emitted


def _drain(timeout: float = 5.0) -> None:
    import time

    import amplifier_module_hooks_memory_capture as m

    if m._QUEUE is None:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if m._QUEUE.unfinished_tasks == 0:
            return
        time.sleep(0.01)
    raise AssertionError("capture queue did not drain within timeout")


# A realistic ~400-byte decision-shaped output, long enough to clear the
# worthiness gate (>50 bytes, <8192 bytes) and to trigger category detection.
_REALISTIC_OUTPUT = (
    "Decided to file drawer captures via the native memory daemon's "
    "client.remember() call rather than a shadow write path. This "
    "architecture removes the dual-write seam that caused drift between "
    "the daemon's index and the on-disk drawer files, and it keeps the "
    "capture pipeline's only I/O dependency on ensure_daemon()."
)
assert 50 < len(_REALISTIC_OUTPUT) < 8192


class TestRealOrchestratorPayloadShape:
    """Feed the hook the verbatim loop-streaming tool:post payload shape."""

    def test_successful_result_dict_is_captured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A ToolResult.model_dump()-shaped success payload must be
        enqueued for capture, not gated as too_short."""
        hook, emitted = _make_hook(monkeypatch, tmp_path)

        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_call_id": "call_123",
                    "tool_input": {"command": "echo hi"},
                    "result": {
                        "success": True,
                        "output": _REALISTIC_OUTPUT,
                        "error": None,
                    },
                    "parallel_group_id": None,
                },
            )
        )

        assert result.action == "continue"

        skipped = [e for e in emitted if e[0][1] == "capture_skipped"]
        assert not skipped, f"Real orchestrator payload was gated as skipped: {skipped}"

        queued = [e for e in emitted if e[0][1] == "capture_queued"]
        assert len(queued) == 1, f"Expected capture_queued in {emitted}"

        _drain()
        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1, f"Expected drawer_filed after drain in {emitted}"
        assert filed[0][1]["data"]["tool_success"] is True

    def test_failed_result_dict_reads_outcome_correctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A ToolResult.model_dump()-shaped failure payload must still be
        captured (failures are memory-worthy), with tool_success=False.

        Real ToolResult.model_post_init auto-populates ``output`` from
        ``error["message"]`` when a tool forgets to set it explicitly, so a
        realistic failure model_dump() always has non-empty output -- this
        payload mirrors that (rather than leaving output=None, which no real
        ToolResult.model_dump() would ever actually produce)."""
        hook, emitted = _make_hook(monkeypatch, tmp_path)

        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_call_id": "call_456",
                    "tool_input": {"command": "false"},
                    "result": {
                        "success": False,
                        "output": _REALISTIC_OUTPUT,
                        "error": {"message": _REALISTIC_OUTPUT},
                    },
                    "parallel_group_id": None,
                },
            )
        )

        assert result.action == "continue"

        skipped = [e for e in emitted if e[0][1] == "capture_skipped"]
        assert not skipped, (
            f"Real orchestrator failure payload was gated as skipped: {skipped}"
        )

        _drain()
        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1, f"Expected drawer_filed after drain in {emitted}"
        assert filed[0][1]["data"]["tool_success"] is False

    def test_error_present_with_no_explicit_success_key_is_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """result.success defaults via `not result.get("error")` when the
        success key itself is absent -- covers ToolResult variants that
        only set error, not success, in their model_dump()."""
        hook, emitted = _make_hook(monkeypatch, tmp_path)

        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_call_id": "call_789",
                    "tool_input": {},
                    "result": {
                        "output": _REALISTIC_OUTPUT,
                        "error": {"message": _REALISTIC_OUTPUT},
                    },
                },
            )
        )

        _drain()
        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1
        assert filed[0][1]["data"]["tool_success"] is False

    def test_non_string_output_is_json_serialized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """ToolResult.output can be any JSON-serializable type (e.g. a dict
        from a structured tool) -- the hook must coerce it to text rather
        than storing str(dict) or dropping it."""
        hook, emitted = _make_hook(monkeypatch, tmp_path)

        structured_output = {
            "decision": "use native daemon writes",
            "rationale": _REALISTIC_OUTPUT,
        }
        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "search",
                    "tool_call_id": "call_999",
                    "tool_input": {},
                    "result": {
                        "success": True,
                        "output": structured_output,
                        "error": None,
                    },
                },
            )
        )

        skipped = [e for e in emitted if e[0][1] == "capture_skipped"]
        assert not skipped, f"structured output was gated as skipped: {skipped}"

        _drain()
        filed = [e for e in emitted if e[0][1] == "drawer_filed"]
        assert len(filed) == 1


class TestExtractOutcomeUnit:
    """Direct unit coverage of _extract_outcome for each supported shape."""

    def test_dict_result_success(self) -> None:
        import amplifier_module_hooks_memory_capture as m

        output, success = m._extract_outcome(
            {"result": {"success": True, "output": "hello world", "error": None}}
        )
        assert output == "hello world"
        assert success is True

    def test_dict_result_failure(self) -> None:
        import amplifier_module_hooks_memory_capture as m

        output, success = m._extract_outcome(
            {
                "result": {
                    "success": False,
                    "output": "boom",
                    "error": {"message": "boom"},
                }
            }
        )
        assert output == "boom"
        assert success is False

    def test_object_result_with_output_attribute(self) -> None:
        """A result object (not dict-serialized) is handled via getattr."""
        import amplifier_module_hooks_memory_capture as m

        class _FakeToolResult:
            success = True
            output = "object-shaped output"
            error = None

        output, success = m._extract_outcome({"result": _FakeToolResult()})
        assert output == "object-shaped output"
        assert success is True

    def test_string_result_fallback(self) -> None:
        """loop-streaming's `str(result)` fallback when model_dump is absent."""
        import amplifier_module_hooks_memory_capture as m

        output, success = m._extract_outcome({"result": "plain string result"})
        assert output == "plain string result"
        assert success is True

    def test_legacy_flat_shape_still_works(self) -> None:
        """Direct/legacy callers using the flat shape are still supported."""
        import amplifier_module_hooks_memory_capture as m

        output, success = m._extract_outcome(
            {"tool_output": "legacy shape", "success": False}
        )
        assert output == "legacy shape"
        assert success is False

    def test_no_result_and_no_legacy_keys_defaults_empty_and_success(self) -> None:
        import amplifier_module_hooks_memory_capture as m

        output, success = m._extract_outcome({})
        assert output == ""
        assert success is True
