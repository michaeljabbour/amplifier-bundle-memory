"""
Unit tests for the MCP-based retrieval lane (_mcp_search, _derive_memory_id)
and the cosine/uncertain-band/llm_judge gating logic in _retrieve_and_gate.

These tests never touch the network or a real mempalace-mcp process --
_call_mcp_tool_impl is monkeypatched throughout.
"""

from __future__ import annotations

import asyncio

import amplifier_module_hooks_mempalace_interject as interject


def _sample_hit(**overrides):
    hit = {
        "text": "We decided to use Rust for the kernel.",
        "wing": "wing_amplifier",
        "room": "decisions",
        "source_file": "HANDOFF.md",
        "source_path": "/repo/project-context/HANDOFF.md",
        "created_at": "2026-01-01",
        "similarity": 0.9,
        "distance": 0.1,
    }
    hit.update(overrides)
    return hit


def test_mcp_search_maps_hits_to_expected_shape(monkeypatch):
    calls = []

    def fake_call(tool_name, arguments, **kwargs):
        calls.append((tool_name, arguments))
        return {
            "query": arguments["query"],
            "results": [_sample_hit()],
        }

    monkeypatch.setattr(interject, "_call_mcp_tool_impl", fake_call)

    memories = interject._mcp_search("rust decision", n_results=5)

    assert len(calls) == 1
    assert calls[0][0] == "mempalace_search"
    assert calls[0][1] == {"query": "rust decision", "limit": 5}

    assert len(memories) == 1
    mem = memories[0]
    assert mem["text"] == "We decided to use Rust for the kernel."
    assert mem["score"] == 0.9
    assert mem["metadata"]["wing"] == "wing_amplifier"
    assert mem["metadata"]["room"] == "decisions"
    assert mem["metadata"]["source_file"] == "HANDOFF.md"
    # No "id" in the raw hit -> a derived surrogate id must be present and stable.
    assert mem["id"] == interject._derive_memory_id(
        _sample_hit(), _sample_hit()["text"]
    )


def test_mcp_search_truncates_query_to_250_chars(monkeypatch):
    captured = {}

    def fake_call(tool_name, arguments, **kwargs):
        captured["arguments"] = arguments
        return {"results": []}

    monkeypatch.setattr(interject, "_call_mcp_tool_impl", fake_call)

    long_query = "x" * 500
    interject._mcp_search(long_query, n_results=5)

    assert len(captured["arguments"]["query"]) == 250


def test_mcp_search_returns_empty_on_error(monkeypatch):
    monkeypatch.setattr(
        interject, "_call_mcp_tool_impl", lambda *a, **k: {"error": "boom"}
    )
    assert interject._mcp_search("anything") == []


def test_mcp_search_returns_empty_on_empty_query(monkeypatch):
    def fake_call(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("_call_mcp_tool_impl should not be called for empty query")

    monkeypatch.setattr(interject, "_call_mcp_tool_impl", fake_call)
    assert interject._mcp_search("") == []


def test_mcp_search_tolerates_malformed_hits(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(), "not-a-dict", None, 42]},
    )
    memories = interject._mcp_search("query")
    assert len(memories) == 1  # only the well-formed dict hit survives


def test_derive_memory_id_stable_for_same_content():
    hit = _sample_hit()
    id1 = interject._derive_memory_id(hit, hit["text"])
    id2 = interject._derive_memory_id(dict(hit), hit["text"])
    assert id1 == id2
    assert len(id1) == 16


def test_derive_memory_id_differs_for_different_content():
    hit_a = _sample_hit(source_path="/repo/a.md")
    hit_b = _sample_hit(source_path="/repo/b.md")
    id_a = interject._derive_memory_id(hit_a, hit_a["text"])
    id_b = interject._derive_memory_id(hit_b, hit_b["text"])
    assert id_a != id_b


def test_retrieve_and_gate_injects_above_cosine_threshold(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(similarity=0.95)]},
    )
    hook = interject.MempalaceInterjectHook({"llm_judge_enabled": False})

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is True
    assert judge_used is False
    assert len(memories) == 1


def test_retrieve_and_gate_below_threshold_is_skipped(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(similarity=0.1)]},
    )
    hook = interject.MempalaceInterjectHook({"llm_judge_enabled": False})

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("unrelated", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "below_threshold"
    assert memories == []


def test_retrieve_and_gate_uncertain_band_without_judge_is_not_promoted(monkeypatch):
    """cosine_threshold=0.72, uncertain_band=0.10 -> uncertain band is
    [0.62, 0.72). With llm_judge_enabled=False (the default), those
    candidates must NOT be promoted -- and the judge must never be called."""

    def fake_call(*a, **k):
        return {"results": [_sample_hit(similarity=0.65)]}

    monkeypatch.setattr(interject, "_call_mcp_tool_impl", fake_call)

    async def judge_should_not_be_called(*a, **k):  # pragma: no cover
        raise AssertionError(
            "llm judge must not be called when llm_judge_enabled=False"
        )

    monkeypatch.setattr(interject, "_llm_judge", judge_should_not_be_called)

    hook = interject.MempalaceInterjectHook({})  # llm_judge_enabled defaults False
    assert hook.llm_judge_enabled is False

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("borderline query", "prompt:submit")
    )
    assert should_inject is False
    assert judge_used is False


def test_retrieve_and_gate_uncertain_band_with_judge_enabled_can_promote(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(similarity=0.65)]},
    )

    async def fake_judge(query, memory_text):
        return 0.9  # above the 0.7 judge threshold

    monkeypatch.setattr(interject, "_llm_judge", fake_judge)

    hook = interject.MempalaceInterjectHook({"llm_judge_enabled": True})
    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("borderline query", "prompt:submit")
    )
    assert judge_used is True
    assert should_inject is True
    assert len(memories) == 1


def test_retrieve_and_gate_retrieval_failed_reason_on_mcp_error(monkeypatch):
    monkeypatch.setattr(
        interject, "_call_mcp_tool_impl", lambda *a, **k: {"error": "transport down"}
    )
    hook = interject.MempalaceInterjectHook({})
    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("anything", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "retrieval_failed"


def test_retrieve_and_gate_respects_cooldown(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(similarity=0.95)]},
    )
    hook = interject.MempalaceInterjectHook({})
    mem_id = interject._derive_memory_id(_sample_hit(), _sample_hit()["text"])
    hook._mark_injected([mem_id])

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "cooldown"


def test_retrieve_and_gate_respects_briefed_ids(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_mcp_tool_impl",
        lambda *a, **k: {"results": [_sample_hit(similarity=0.95)]},
    )
    hook = interject.MempalaceInterjectHook({})
    mem_id = interject._derive_memory_id(_sample_hit(), _sample_hit()["text"])
    hook._briefed_ids.add(mem_id)

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "cooldown"
