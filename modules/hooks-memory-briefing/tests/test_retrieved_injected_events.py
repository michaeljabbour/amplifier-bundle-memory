"""
Tests for T4.1 ``memory:retrieved``/``memory:injected`` emission from
hooks-memory-briefing.

One ``memory:retrieved`` event per briefing assembly (mechanism-first
aggregate of the pass's searches/fact/index reads); one ``memory:injected``
when the briefing actually gets injected into context.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_briefing as briefing_mod
import pytest
from amplifier_module_hooks_memory_briefing import MemoryBriefingHook

SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeClientWithIndex:
    def index(self, **kw: Any) -> Any:  # pragma: no cover
        return None


class _FakeClientWithoutIndex:
    """Shaped like an older tool-memory pin (no L3 ops) -- legacy path."""


def _install_fake_daemon(
    monkeypatch: pytest.MonkeyPatch, client: Any, responses: dict[str, Any]
) -> None:
    monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: client)
    monkeypatch.setattr(
        briefing_mod, "_call_client", lambda method, **kw: responses.get(method)
    )
    monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
    monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)


class TestLayeredBriefingEmitsRetrievedAndInjected:
    def test_retrieved_and_injected_fire_with_expected_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            briefing_mod,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [
                    {
                        "scope": "room:r",
                        "abstract": f"secret {SECRET}",
                        "pending_changes": 0,
                    }
                ],
                "standing": [],
                "facts": [
                    {"ref": "fact1", "text": f"fact text {SECRET}", "proof_count": 1}
                ],
                "search": {
                    "results": [
                        {
                            "ref": "drawer1",
                            "score": 0.9,
                            "content": f"evidence {SECRET}",
                            "room": "r",
                        }
                    ]
                },
            },
        )
        hook = MemoryBriefingHook({"briefing_mode": "layered"})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": "q"}))
        assert result.action == "inject_context"

        retrieved = [d for e, d in captured if e == "retrieved"]
        assert len(retrieved) == 1
        assert retrieved[0]["source"] == "briefing"
        assert retrieved[0]["op"] == "layered"
        assert isinstance(retrieved[0]["latency_ms"], float)
        refs = {h["ref"] for h in retrieved[0]["hits"]}
        assert "room:r" in refs
        assert "fact1" in refs
        assert "drawer1" in refs
        assert SECRET not in str(retrieved[0])

        injected = [d for e, d in captured if e == "injected"]
        assert len(injected) == 1
        assert injected[0]["source"] == "briefing"
        assert "drawer1" in injected[0]["refs"]
        assert injected[0]["layers"]["drawer1"] == "drawer"
        assert injected[0]["chars"] > 0
        assert SECRET not in str(injected[0])

    def test_hits_capped_at_20(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            briefing_mod,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [
                    {"scope": f"room:{i}", "abstract": "x", "pending_changes": 0}
                    for i in range(30)
                ],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        hook = MemoryBriefingHook({"briefing_mode": "layered"})
        _run(hook("session:start", {"session_id": "s1", "prompt": "q"}))

        retrieved = [d for e, d in captured if e == "retrieved"][0]
        assert len(retrieved["hits"]) == 20

    def test_emit_events_false_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_calls: list[Any] = []
        bridge_calls: list[Any] = []
        monkeypatch.setattr(
            briefing_mod, "emit_event", lambda *a, **k: emit_calls.append((a, k))
        )
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [{"scope": "room:r", "abstract": "x", "pending_changes": 0}],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        hook = MemoryBriefingHook(
            {"briefing_mode": "layered"},
            bridge_emit=lambda event, payload: bridge_calls.append((event, payload)),
        )
        hook.emit_events = False
        result = _run(hook("session:start", {"session_id": "s1", "prompt": "q"}))
        assert result.action == "inject_context"
        assert emit_calls == []
        assert bridge_calls == []

    def test_emission_failure_never_breaks_briefing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom_for_new_events(hook: str, event: str, *a: Any, **k: Any) -> None:
            if event in ("retrieved", "injected"):
                raise RuntimeError("observability down")

        monkeypatch.setattr(briefing_mod, "emit_event", _boom_for_new_events)
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [{"scope": "room:r", "abstract": "x", "pending_changes": 0}],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        hook = MemoryBriefingHook({"briefing_mode": "layered"})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": "q"}))
        assert result.action == "inject_context"


class TestLegacyBriefingEmitsRetrieved:
    def test_legacy_mode_emits_retrieved_with_op_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            briefing_mod,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithoutIndex(),
            {
                "search": {
                    "results": [
                        {
                            "ref": "drawer1",
                            "score": 0.9,
                            "content": f"text {SECRET}",
                            "room": "r",
                        }
                    ]
                },
                "kg_query": [],
                "diary_read": [],
            },
        )
        hook = MemoryBriefingHook({"briefing_mode": "layered"})  # falls back to legacy
        result = _run(hook("session:start", {"session_id": "s1", "prompt": "q"}))
        assert result.action == "inject_context"

        retrieved = [d for e, d in captured if e == "retrieved"]
        assert len(retrieved) == 1
        assert retrieved[0]["op"] == "legacy"
        assert SECRET not in str(retrieved[0])
