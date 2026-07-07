"""
Unit tests for the native retrieval lane (_mcp_search, _derive_memory_id)
and the cosine/uncertain-band/llm_judge gating logic in _retrieve_and_gate.

Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): these
tests never touch the network or a real daemon -- _call_client (the ONE
transport seam to MemoryClient via ensure_daemon()) is monkeypatched
throughout, mirroring the shape the native daemon's `search` domain tool
actually returns: {results: [{ref, score, content, wing, room, category,
source}], degraded}.
"""

from __future__ import annotations

import asyncio

import amplifier_module_hooks_memory_interject as interject


def _sample_hit(**overrides):
    hit = {
        "ref": "abc123def456",
        "content": "We decided to use Rust for the kernel.",
        "wing": "wing_amplifier",
        "room": "decisions",
        "source": "HANDOFF.md",
        "score": 0.9,
    }
    hit.update(overrides)
    return hit


def test_mcp_search_maps_hits_to_expected_shape(monkeypatch):
    calls = []

    def fake_call(method, **kwargs):
        calls.append((method, kwargs))
        return {"results": [_sample_hit()], "degraded": None}

    monkeypatch.setattr(interject, "_call_client", fake_call)

    memories = interject._mcp_search("rust decision", n_results=5)

    assert len(calls) == 1
    assert calls[0][0] == "search"
    assert calls[0][1] == {"query": "rust decision", "k": 5}

    assert len(memories) == 1
    mem = memories[0]
    assert mem["text"] == "We decided to use Rust for the kernel."
    assert mem["score"] == 0.9
    assert mem["metadata"]["wing"] == "wing_amplifier"
    assert mem["metadata"]["room"] == "decisions"
    assert mem["metadata"]["source_file"] == "HANDOFF.md"
    # Native search always returns a real ref -- used directly as the id.
    assert mem["id"] == "abc123def456"


def test_mcp_search_truncates_query_to_250_chars(monkeypatch):
    captured = {}

    def fake_call(method, **kwargs):
        captured["kwargs"] = kwargs
        return {"results": []}

    monkeypatch.setattr(interject, "_call_client", fake_call)

    long_query = "x" * 500
    interject._mcp_search(long_query, n_results=5)

    assert len(captured["kwargs"]["query"]) == 250


def test_mcp_search_returns_empty_on_daemon_unavailable(monkeypatch):
    monkeypatch.setattr(interject, "_call_client", lambda *a, **k: None)
    assert interject._mcp_search("anything") == []


def test_mcp_search_returns_empty_on_empty_query(monkeypatch):
    def fake_call(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("_call_client should not be called for empty query")

    monkeypatch.setattr(interject, "_call_client", fake_call)
    assert interject._mcp_search("") == []


def test_mcp_search_tolerates_malformed_hits(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(), "not-a-dict", None, 42]},
    )
    memories = interject._mcp_search("query")
    assert len(memories) == 1  # only the well-formed dict hit survives


def test_mcp_search_falls_back_to_derived_id_when_ref_missing(monkeypatch):
    """Defensive path: a malformed hit lacking `ref` still gets a stable
    surrogate id via _derive_memory_id."""
    hit = _sample_hit()
    del hit["ref"]
    monkeypatch.setattr(interject, "_call_client", lambda *a, **k: {"results": [hit]})
    memories = interject._mcp_search("query")
    assert len(memories) == 1
    assert memories[0]["id"] == interject._derive_memory_id(hit, hit["content"])


def test_derive_memory_id_stable_for_same_content():
    hit = _sample_hit(source_path="/repo/a.md")
    id1 = interject._derive_memory_id(hit, hit["content"])
    id2 = interject._derive_memory_id(dict(hit), hit["content"])
    assert id1 == id2
    assert len(id1) == 16


def test_derive_memory_id_differs_for_different_content():
    hit_a = _sample_hit(source_path="/repo/a.md")
    hit_b = _sample_hit(source_path="/repo/b.md")
    id_a = interject._derive_memory_id(hit_a, hit_a["content"])
    id_b = interject._derive_memory_id(hit_b, hit_b["content"])
    assert id_a != id_b


def test_retrieve_and_gate_injects_above_cosine_threshold(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(score=0.95)]},
    )
    hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is True
    assert judge_used is False
    assert len(memories) == 1


def test_retrieve_and_gate_below_threshold_is_skipped(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(score=0.1)]},
    )
    hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})

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
        return {"results": [_sample_hit(score=0.65)]}

    monkeypatch.setattr(interject, "_call_client", fake_call)

    async def judge_should_not_be_called(*a, **k):  # pragma: no cover
        raise AssertionError(
            "llm judge must not be called when llm_judge_enabled=False"
        )

    monkeypatch.setattr(interject, "_llm_judge", judge_should_not_be_called)

    hook = interject.MemoryInterjectHook({})  # llm_judge_enabled defaults False
    assert hook.llm_judge_enabled is False

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("borderline query", "prompt:submit")
    )
    assert should_inject is False
    assert judge_used is False


def test_retrieve_and_gate_uncertain_band_with_judge_enabled_can_promote(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(score=0.65)]},
    )

    async def fake_judge(query, memory_text):
        return 0.9  # above the 0.7 judge threshold

    monkeypatch.setattr(interject, "_llm_judge", fake_judge)

    hook = interject.MemoryInterjectHook({"llm_judge_enabled": True})
    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("borderline query", "prompt:submit")
    )
    assert judge_used is True
    assert should_inject is True
    assert len(memories) == 1


def test_retrieve_and_gate_retrieval_failed_reason_on_daemon_unavailable(monkeypatch):
    monkeypatch.setattr(interject, "_call_client", lambda *a, **k: None)
    hook = interject.MemoryInterjectHook({})
    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("anything", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "retrieval_failed"


def test_retrieve_and_gate_respects_cooldown(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(score=0.95)]},
    )
    hook = interject.MemoryInterjectHook({})
    mem_id = _sample_hit()["ref"]
    hook._mark_injected([mem_id])

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "cooldown"


def test_retrieve_and_gate_respects_briefed_ids(monkeypatch):
    monkeypatch.setattr(
        interject,
        "_call_client",
        lambda *a, **k: {"results": [_sample_hit(score=0.95)]},
    )
    hook = interject.MemoryInterjectHook({})
    mem_id = _sample_hit()["ref"]
    hook._briefed_ids.add(mem_id)

    memories, should_inject, skip_reason, judge_used = asyncio.run(
        hook._retrieve_and_gate("rust decision", "prompt:submit")
    )
    assert should_inject is False
    assert skip_reason == "cooldown"


# ---------------------------------------------------------------------------
# Structural guard (folded in from the deleted test_store_alignment.py, \u00a73.2
# of the native-cutover design -- the underlying regression class, "this
# module must never touch a store directly," still matters post-cutover):
# the module must not import chromadb or construct a PersistentClient, and
# must not resurrect the old direct-embedding/direct-cosine helpers.
# ---------------------------------------------------------------------------


def test_module_never_hardcodes_a_vector_store_at_runtime():
    import ast
    import inspect

    source = inspect.getsource(interject)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name == "chromadb" for alias in node.names), (
                "module must not import chromadb"
            )
        if isinstance(node, ast.ImportFrom):
            assert node.module != "chromadb", "module must not import from chromadb"
        if isinstance(node, ast.Attribute) and node.attr == "PersistentClient":
            raise AssertionError(
                "module must not construct a chromadb PersistentClient"
            )

    assert not hasattr(interject, "_retrieve_memories")
    assert not hasattr(interject, "_embed")
    assert not hasattr(interject, "_cosine")
    assert hasattr(interject, "_mcp_search")
