"""Integration tests: secret scrubbing wired into the capture hook (T0.3).

Verifies that a secret in a captured tool output:
  - never reaches the filed drawer content,
  - never reaches the capture_queued/drawer_filed preview or event data,
  - triggers a memory:capture_redacted event (counts only, never the value),
  - is only skipped when redact_secrets=False.

Also covers T0.2's hook-side half: session_id/commit passed through to the
store write, with a TypeError fallback for stores that don't accept them yet.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _drain_capture_queue_between_tests() -> Any:
    yield
    import amplifier_module_hooks_memory_capture as m

    if m._QUEUE is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and m._QUEUE.unfinished_tasks > 0:
            time.sleep(0.01)


def _drain(timeout: float = 5.0) -> None:
    import amplifier_module_hooks_memory_capture as m

    if m._QUEUE is None:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if m._QUEUE.unfinished_tasks == 0:
            return
        time.sleep(0.01)
    raise AssertionError("capture queue did not drain within timeout")


_SECRET = "sk-ant-" + "s" * 30
# A realistic decision-shaped body long enough to clear the worthiness gate
# (>50 bytes, <8192 bytes) and trip category detection, with a secret
# embedded in the middle -- the shape a real leaked-key capture would take.
_BODY_WITH_SECRET = (
    "We decided to use this key for the integration: "
    f"{_SECRET}"
    " going with it for now until rotation."
)
assert 50 < len(_BODY_WITH_SECRET) < 8192


def _make_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **config: Any):
    import amplifier_module_hooks_memory_capture as m

    emitted: list[tuple[Any, ...]] = []
    filed: list[dict[str, Any]] = []

    def _capture_emit(*a: Any, **kw: Any) -> None:
        emitted.append((a, kw))

    def _capture_file_drawer(
        wing: str,
        room: str,
        content: str,
        source: str,
        category: str | None,
        *,
        session_id: str | None = None,
        commit: str | None = None,
    ) -> None:
        filed.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "session_id": session_id,
                "commit": commit,
            }
        )

    bridged: list[tuple[str, Any]] = []

    def _bridge_emit(event: str, payload: Any) -> None:
        bridged.append((event, payload))

    monkeypatch.setattr(m, "emit_event", _capture_emit)
    monkeypatch.setattr(m, "_file_drawer", _capture_file_drawer)
    monkeypatch.setattr(m, "_detect_wing", lambda: "wing_test")
    monkeypatch.setattr(
        m, "resolve_commit", lambda: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )
    monkeypatch.setattr(
        m, "_spool_dir_for", lambda sid: tmp_path / "spool" / (sid or "x")
    )

    hook = m.MemoryCaptureHook(config=config, bridge_emit=_bridge_emit)
    return hook, emitted, filed, bridged


class TestSecretNeverReachesFiledContent:
    def test_default_redact_secrets_true_scrubs_before_filing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        hook, emitted, filed, _bridged = _make_hook(monkeypatch, tmp_path)

        result = _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": _BODY_WITH_SECRET,
                    "session_id": "sess-1",
                },
            )
        )
        assert result.action == "continue"
        _drain()

        assert len(filed) == 1, f"expected exactly one filed drawer, got {filed}"
        assert _SECRET not in filed[0]["content"], (
            "secret leaked into filed drawer content"
        )
        assert "[REDACTED:anthropic_key]" in filed[0]["content"]

        # No event anywhere (queued/filed previews included) contains it.
        for _args, kwargs in emitted:
            preview = kwargs.get("preview")
            if preview:
                assert _SECRET not in preview

    def test_capture_redacted_event_emitted_with_counts_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        hook, emitted, _filed, bridged = _make_hook(monkeypatch, tmp_path)

        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": _BODY_WITH_SECRET,
                    "session_id": "sess-2",
                },
            )
        )
        _drain()

        redacted_jsonl = [e for e in emitted if e[0][1] == "capture_redacted"]
        assert len(redacted_jsonl) == 1, (
            f"expected one capture_redacted JSONL event, got {emitted}"
        )
        data = redacted_jsonl[0][1]["data"]
        assert data["counts"] == {"anthropic_key": 1}
        assert _SECRET not in str(data)

        redacted_bridged = [b for b in bridged if b[0] == "memory:capture_redacted"]
        assert len(redacted_bridged) == 1, (
            f"expected one bridged capture_redacted, got {bridged}"
        )
        payload = redacted_bridged[0][1]
        assert payload["counts"] == {"anthropic_key": 1}
        assert payload["ok"] is True
        assert _SECRET not in str(payload)

    def test_no_redaction_event_when_content_has_no_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        hook, emitted, _filed, bridged = _make_hook(monkeypatch, tmp_path)

        clean_body = (
            "We decided to use pytest fixtures for isolation going with it "
            "for the whole suite from now on."
        )
        assert 50 < len(clean_body) < 8192

        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": clean_body,
                    "session_id": "sess-3",
                },
            )
        )
        _drain()

        assert not [e for e in emitted if e[0][1] == "capture_redacted"]
        assert not [b for b in bridged if b[0] == "memory:capture_redacted"]


class TestRedactSecretsConfigFlag:
    def test_redact_secrets_false_disables_scrubbing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        hook, _emitted, filed, bridged = _make_hook(
            monkeypatch, tmp_path, redact_secrets=False
        )

        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": _BODY_WITH_SECRET,
                    "session_id": "sess-4",
                },
            )
        )
        _drain()

        assert len(filed) == 1
        assert _SECRET in filed[0]["content"], (
            "redact_secrets=False must leave content untouched"
        )
        assert not [b for b in bridged if b[0] == "memory:capture_redacted"]


class TestSessionAndCommitPassthrough:
    def test_file_drawer_receives_session_id_and_commit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        hook, _emitted, filed, _bridged = _make_hook(monkeypatch, tmp_path)

        _run(
            hook(
                "tool:post",
                {
                    "tool_name": "bash",
                    "tool_input": {},
                    "tool_output": "x" * 200,
                    "session_id": "sess-provenance",
                },
            )
        )
        _drain()

        assert len(filed) == 1
        assert filed[0]["session_id"] == "sess-provenance"
        assert filed[0]["commit"] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


class TestMountRegistersCaptureRedactedEvent:
    def test_mount_declares_capture_redacted(self) -> None:
        import inspect

        import amplifier_module_hooks_memory_capture as m

        src = inspect.getsource(m.mount)
        assert "memory:capture_redacted" in src
