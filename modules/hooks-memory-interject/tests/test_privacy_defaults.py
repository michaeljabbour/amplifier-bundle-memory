"""
Privacy defaults for hooks-memory-interject.

By default this hook must make ZERO external network calls: retrieval goes
through the native memory daemon's own local `search` (no query embedding
computed by this hook at all -- the daemon embeds server-side), and the
only optional external call -- the OpenAI LLM judge -- must default to
disabled.
"""

from __future__ import annotations

import asyncio

import amplifier_module_hooks_memory_interject as interject


def test_llm_judge_enabled_defaults_to_false():
    hook = interject.MemoryInterjectHook({})
    assert hook.llm_judge_enabled is False


def test_llm_judge_enabled_defaults_to_false_with_other_config_present():
    hook = interject.MemoryInterjectHook({"cosine_threshold": 0.5})
    assert hook.llm_judge_enabled is False


def test_llm_judge_enabled_can_be_explicitly_opted_in():
    hook = interject.MemoryInterjectHook({"llm_judge_enabled": True})
    assert hook.llm_judge_enabled is True


def test_no_openai_embedding_helper_exists_anymore():
    """The old _embed() function called OpenAI unconditionally on every
    retrieval. It must no longer exist -- retrieval never embeds locally;
    the native daemon does its own (local) embedding server-side."""
    assert not hasattr(interject, "_embed")


def test_llm_judge_is_the_only_openai_reference_in_the_module():
    """Structural guard: 'openai' may only appear inside _llm_judge (the
    opt-in path) -- never in the retrieval path (_mcp_search /
    _retrieve_and_gate's primary gate)."""
    import inspect

    retrieval_source = inspect.getsource(interject._mcp_search)
    assert "openai" not in retrieval_source.lower()

    gate_source = inspect.getsource(interject.MemoryInterjectHook._retrieve_and_gate)
    # openai must only be reachable via the llm_judge_enabled-gated branch
    assert "from openai" not in gate_source


def test_llm_judge_not_invoked_when_disabled_even_with_uncertain_candidates(
    monkeypatch,
):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {
            "results": [
                {
                    "ref": "borderline-ref",
                    "content": "borderline memory",
                    "wing": "w",
                    "room": "r",
                    "source": "f.md",
                    "score": 0.65,  # in the default uncertain band [0.62, 0.72)
                }
            ]
        },
    )
    judge_calls = []

    async def tracking_judge(query, memory_text):
        judge_calls.append((query, memory_text))
        return 1.0

    monkeypatch.setattr(interject, "_llm_judge", tracking_judge)

    hook = interject.MemoryInterjectHook({})  # llm_judge_enabled default False
    asyncio.run(hook._retrieve_and_gate("query", "prompt:submit"))

    assert judge_calls == []


def test_mount_config_reports_llm_judge_enabled_default(monkeypatch):
    class _FakeHooks:
        def register(self, *a, **k):
            pass

    class _FakeCoordinator:
        hooks = _FakeHooks()

    result = asyncio.run(interject.mount(_FakeCoordinator(), {}))
    assert result["config"]["llm_judge_enabled"] is False
    assert "collection" not in result["config"]
    assert "collection" not in result["config"]
