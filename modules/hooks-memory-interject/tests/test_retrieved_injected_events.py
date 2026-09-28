"""
Tests for T4.1 ``memory:retrieved``/``memory:injected`` emission from
hooks-memory-interject.

``memory:retrieved`` fires on every retrieval attempt (via
``_retrieve_and_gate``), regardless of whether it ends up injecting.
``memory:injected`` fires only when content actually enters the model
context (the three ``on_*`` handlers, when they return an
``inject_context`` HookResult).
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_interject as interject

SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"


def _hit(**overrides: Any) -> dict[str, Any]:
    hit = {
        "ref": "abc123",
        "content": f"decision text containing {SECRET}",
        "wing": "wing_x",
        "room": "decisions",
        "source": "HANDOFF.md",
        "score": 0.95,
        "layer": "drawer",
        "rrf": 0.4,
        "arms": {"semantic": 1},
    }
    hit.update(overrides)
    return hit


class TestRetrievedFiresOnEveryAttempt:
    def test_retrieved_fires_even_when_below_threshold(self, monkeypatch) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.1)]}
        )
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})

        _memories, should_inject, _reason, _judge = asyncio.run(
            hook._retrieve_and_gate(
                "unrelated query", "prompt:submit", trigger="prompt_submit", sid="s1"
            )
        )
        assert should_inject is False

        retrieved = [d for e, d in captured if e == "retrieved"]
        assert len(retrieved) == 1
        assert retrieved[0]["source"] == "interject"
        assert retrieved[0]["op"] == "prompt_submit"
        assert retrieved[0]["query"] == "unrelated query"

    def test_retrieved_hits_never_contain_content(self, monkeypatch) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.95)]}
        )
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})
        asyncio.run(
            hook._retrieve_and_gate(
                "rust decision", "prompt:submit", trigger="prompt_submit", sid="s1"
            )
        )

        retrieved = [d for e, d in captured if e == "retrieved"][0]
        assert SECRET not in str(retrieved)
        assert retrieved["hits"][0]["ref"] == "abc123"
        assert retrieved["hits"][0]["layer"] == "drawer"
        assert "rrf" in retrieved["hits"][0]

    def test_query_redacted_in_retrieved_event(self, monkeypatch) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(interject, "_call_client", lambda *a, **k: {"results": []})
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({})
        asyncio.run(
            hook._retrieve_and_gate(
                f"leaked key {SECRET}",
                "prompt:submit",
                trigger="prompt_submit",
                sid=None,
            )
        )
        retrieved = [d for e, d in captured if e == "retrieved"][0]
        assert SECRET not in retrieved["query"]

    def test_hits_capped_at_20(self, monkeypatch) -> None:
        captured: list[dict] = []
        many_hits = {"results": [_hit(ref=f"r{i}", score=0.95) for i in range(30)]}
        monkeypatch.setattr(interject, "_call_client", lambda *a, **k: many_hits)
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({})
        asyncio.run(
            hook._retrieve_and_gate("q", "prompt:submit", trigger="prompt_submit")
        )
        retrieved = [d for e, d in captured if e == "retrieved"][0]
        assert len(retrieved["hits"]) == 20

    def test_emit_events_false_is_a_noop(self, monkeypatch) -> None:
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.95)]}
        )
        monkeypatch.setattr(
            interject, "emit_event", lambda *a, **k: emit_calls.append((a, k))
        )
        hook = interject.MemoryInterjectHook({"emit_events": False})
        memories, should_inject, _reason, _judge = asyncio.run(
            hook._retrieve_and_gate("q", "prompt:submit", trigger="prompt_submit")
        )
        assert should_inject is True  # retrieval/gating still functions
        assert emit_calls == []

    def test_emission_failure_never_breaks_retrieval(self, monkeypatch) -> None:
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.95)]}
        )

        def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("observability down")

        monkeypatch.setattr(interject, "emit_event", _boom)
        hook = interject.MemoryInterjectHook({})
        memories, should_inject, _reason, _judge = asyncio.run(
            hook._retrieve_and_gate("q", "prompt:submit", trigger="prompt_submit")
        )
        assert should_inject is True
        assert len(memories) == 1


class TestInjectedFiresOnlyWhenContentEntersContext:
    def test_injected_fires_on_successful_prompt_submit_injection(
        self, monkeypatch
    ) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.95)]}
        )
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})
        result = asyncio.run(
            hook.on_prompt_submit(
                "prompt:submit",
                {"session_id": "s1", "prompt": "a long enough prompt to pass gate"},
            )
        )
        assert result.action == "inject_context"

        injected = [d for e, d in captured if e == "injected"]
        assert len(injected) == 1
        assert injected[0]["source"] == "interject"
        assert injected[0]["refs"] == ["abc123"]
        assert injected[0]["layers"] == {"abc123": "drawer"}
        assert injected[0]["chars"] > 0

    def test_injected_does_not_fire_when_below_threshold(self, monkeypatch) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [_hit(score=0.1)]}
        )
        monkeypatch.setattr(
            interject,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append((event, data or {}))
            ),
        )
        hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})
        result = asyncio.run(
            hook.on_prompt_submit(
                "prompt:submit",
                {"session_id": "s1", "prompt": "a long enough prompt to pass gate"},
            )
        )
        assert result.action == "continue"
        assert [e for e, _d in captured if e == "injected"] == []
