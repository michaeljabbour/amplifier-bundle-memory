"""T6.3 -- date-stamped, passage-aware evidence rendering.

Covers the pure ``_render_hit``/``_date_stamp`` helpers (dates, facts,
passages, missing dates), ``_format_injection``'s use of the helper
(max_inject_chars accounting includes the stamp), passage-granularity
retrieval, and the ``_call_client`` capability fallback (retry without
``granularity`` on ``TypeError``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_interject as interject
from amplifier_core import HookRegistry  # type: ignore[import]


def _mem(text: str, **overrides: Any) -> dict[str, Any]:
    hit = {"id": text[:8], "text": text, "score": 0.9, "metadata": {}}
    hit.update(overrides)
    return hit


# ---------------------------------------------------------------------------
# _date_stamp / _render_hit -- pure unit tests
# ---------------------------------------------------------------------------


class TestDateStamp:
    def test_prefers_observed_at_over_filed_at(self) -> None:
        hit = {
            "filed_at": "2026-01-01T00:00:00+00:00",
            "observed_at": "2020-05-05T00:00:00+00:00",
        }
        assert interject._date_stamp(hit) == "2020-05-05"

    def test_falls_back_to_filed_at(self) -> None:
        assert (
            interject._date_stamp({"filed_at": "2026-09-28T12:00:00+00:00"})
            == "2026-09-28"
        )

    def test_missing_both_returns_empty(self) -> None:
        assert interject._date_stamp({}) == ""

    def test_malformed_value_returns_empty(self) -> None:
        assert interject._date_stamp({"filed_at": "garbage"}) == ""


class TestRenderHit:
    def test_plain_memory_with_no_layer_or_date_is_unchanged(self) -> None:
        """The pre-T6.3 shape ({id, text, score, metadata}) must render
        identically to its own text -- this is what keeps every existing
        interject test byte-for-byte unaffected."""
        mem = _mem("plain memory content")
        assert interject._render_hit(mem) == "plain memory content"

    def test_plain_memory_with_date(self) -> None:
        mem = _mem("some content", filed_at="2026-09-28T00:00:00+00:00")
        assert interject._render_hit(mem) == "[2026-09-28] some content"

    def test_fact_memory_renders_proof_count(self) -> None:
        mem = _mem(
            "the repo uses uv",
            layer="fact",
            proof_count=2,
            filed_at="2026-01-01T00:00:00+00:00",
        )
        assert (
            interject._render_hit(mem)
            == "[2026-01-01] (fact, proof 2) the repo uses uv"
        )

    def test_fact_memory_without_date(self) -> None:
        mem = _mem("x", layer="fact", proof_count=1)
        assert interject._render_hit(mem) == "(fact, proof 1) x"

    def test_passage_memory_gets_provenance_tag_by_default(self) -> None:
        mem = _mem("a passage snippet", layer="passage", drawer_ref="abcdef1234567890")
        assert interject._render_hit(mem) == "a passage snippet (from abcdef12)"

    def test_passage_memory_tag_can_be_disabled(self) -> None:
        mem = _mem("a passage snippet", layer="passage", drawer_ref="abcdef1234567890")
        assert interject._render_hit(mem, provenance_tag=False) == "a passage snippet"

    def test_passage_memory_without_drawer_ref_has_no_tag(self) -> None:
        mem = _mem("a passage snippet", layer="passage")
        assert interject._render_hit(mem) == "a passage snippet"

    def test_passage_memory_with_date_and_tag(self) -> None:
        mem = _mem(
            "a passage snippet",
            layer="passage",
            drawer_ref="abcdef1234567890",
            observed_at="2026-03-15T00:00:00+00:00",
        )
        assert (
            interject._render_hit(mem)
            == "[2026-03-15] a passage snippet (from abcdef12)"
        )


# ---------------------------------------------------------------------------
# _format_injection now renders via _render_hit -- max_inject_chars
# accounting must include the stamp/tag.
# ---------------------------------------------------------------------------


class TestFormatInjectionRendersDates:
    def test_date_stamp_counts_against_the_cap(self) -> None:
        mem = _mem("x" * 780, filed_at="2026-09-28T00:00:00+00:00")
        out = interject._format_injection([mem], HookRegistry.PROMPT_SUBMIT, 800)
        # "[2026-09-28] " is 13 chars; without accounting for it the old
        # code would have let the total run past 800.
        assert len(out) <= 800

    def test_passage_provenance_tag_counts_against_the_cap(self) -> None:
        mem = _mem(
            "y" * 780,
            layer="passage",
            drawer_ref="abcdef1234567890",
        )
        out = interject._format_injection([mem], HookRegistry.PROMPT_SUBMIT, 800)
        assert len(out) <= 800

    def test_plain_memories_still_render_unchanged(self) -> None:
        """Pinned: existing behavior for memories with no layer/date field
        is untouched."""
        out = interject._format_injection(
            [_mem("first memory"), _mem("second memory")],
            HookRegistry.PROMPT_SUBMIT,
            800,
        )
        assert "first memory" in out and "second memory" in out


# ---------------------------------------------------------------------------
# _call_client capability fallback (real function, fake ensure_daemon)
# ---------------------------------------------------------------------------


class _ClientNoGranularity:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, *, query: str, k: int) -> Any:
        self.calls.append({"query": query, "k": k})
        return {"results": [{"ref": "d1", "content": "fallback result"}]}


class TestCallClientCapabilityFallback:
    def test_retries_without_granularity_on_type_error(self, monkeypatch) -> None:
        client = _ClientNoGranularity()
        monkeypatch.setattr(interject, "ensure_daemon", lambda: client)

        result = interject._call_client("search", query="q", k=5, granularity="passage")

        assert result == {"results": [{"ref": "d1", "content": "fallback result"}]}
        assert len(client.calls) == 1

    def test_other_type_errors_still_surface_as_none(self, monkeypatch) -> None:
        class _Client:
            def search(self, **kwargs: Any) -> Any:
                raise TypeError("completely unrelated bug")

        monkeypatch.setattr(interject, "ensure_daemon", lambda: _Client())
        assert interject._call_client("search", query="q", k=5) is None


# ---------------------------------------------------------------------------
# _mcp_search: granularity kwarg is opt-in and only reaches _call_client
# when explicitly requested (backward-compat with the pinned kwarg-equality
# test in test_retrieval.py).
# ---------------------------------------------------------------------------


class TestMcpSearchGranularity:
    def test_granularity_omitted_by_default(self, monkeypatch) -> None:
        captured = {}

        def fake_call(method, **kwargs):
            captured["kwargs"] = kwargs
            return {"results": []}

        monkeypatch.setattr(interject, "_call_client", fake_call)
        interject._mcp_search("query", n_results=5)
        assert "granularity" not in captured["kwargs"]

    def test_granularity_passed_through_when_requested(self, monkeypatch) -> None:
        captured = {}

        def fake_call(method, **kwargs):
            captured["kwargs"] = kwargs
            return {"results": []}

        monkeypatch.setattr(interject, "_call_client", fake_call)
        interject._mcp_search("query", n_results=5, granularity="passage")
        assert captured["kwargs"]["granularity"] == "passage"

    def test_end_to_end_capability_fallback_via_real_call_client(
        self, monkeypatch
    ) -> None:
        """Full path: ensure_daemon() returns an old-style client whose
        search() has no granularity param -- _mcp_search must still return
        results via the real (unmocked) _call_client retry."""

        class _OldStyleClient:
            def search(self, *, query: str, k: int) -> Any:
                return {
                    "results": [
                        {"ref": "d1", "content": "old-style result", "score": 0.8}
                    ]
                }

        monkeypatch.setattr(interject, "ensure_daemon", lambda: _OldStyleClient())
        memories = interject._mcp_search("query", granularity="passage")
        assert len(memories) == 1
        assert memories[0]["text"] == "old-style result"

    def test_new_fields_propagated_through(self, monkeypatch) -> None:
        hit = {
            "ref": "p1",
            "content": "a passage",
            "score": 0.9,
            "layer": "passage",
            "drawer_ref": "abcdef12",
            "span": [0, 10],
            "filed_at": "2026-09-28T00:00:00+00:00",
            "observed_at": "2026-09-27T00:00:00+00:00",
        }
        monkeypatch.setattr(
            interject, "_call_client", lambda *a, **k: {"results": [hit]}
        )
        memories = interject._mcp_search("query")
        assert len(memories) == 1
        mem = memories[0]
        assert mem["layer"] == "passage"
        assert mem["drawer_ref"] == "abcdef12"
        assert mem["span"] == [0, 10]
        assert mem["filed_at"] == "2026-09-28T00:00:00+00:00"
        assert mem["observed_at"] == "2026-09-27T00:00:00+00:00"


# ---------------------------------------------------------------------------
# End-to-end: on_prompt_submit requests passage granularity and renders it.
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestPromptSubmitUsesPassageGranularity:
    def test_prompt_submit_requests_passage_granularity(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        def fake_call(method, **kwargs):
            captured["granularity"] = kwargs.get("granularity")
            return {
                "results": [
                    {
                        "ref": "p1",
                        "content": "dated passage content",
                        "score": 0.95,
                        "layer": "passage",
                        "drawer_ref": "abcdef1234567890",
                        "filed_at": "2026-09-28T00:00:00+00:00",
                    }
                ]
            }

        monkeypatch.setattr(interject, "_call_client", fake_call)
        hook = interject.MemoryInterjectHook({"llm_judge_enabled": False})
        result = _run(
            hook.on_prompt_submit(
                "prompt:submit",
                {"prompt": "a sufficiently long prompt to pass the length gate"},
            )
        )
        assert captured["granularity"] == "passage"
        assert result.action == "inject_context"
        assert "[2026-09-28] dated passage content (from abcdef12)" in (
            result.context_injection
        )
