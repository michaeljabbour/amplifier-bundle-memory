"""
Tests for T4.1 ``memory:retrieved`` emission from MemoryTool's
search/facts/index operations.

Covers: payload shape, no content leakage, query redaction, hit cap (20),
emit_events=False is a no-op, and emission failures never break the op.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_tool_memory as tm
import pytest
from amplifier_module_tool_memory import MemoryTool
from amplifier_module_tool_memory.event_emitter import (
    build_retrieved_data,
    summarize_hits,
)


def _run(coro):
    return asyncio.run(coro)


SECRET_QUERY = "use key sk-ant-abcdefghijklmnopqrstuvwxyz012345 to auth"
SECRET_TEXT = "the api key is sk-ant-abcdefghijklmnopqrstuvwxyz012345"


def _search_hits(n: int) -> list[dict[str, Any]]:
    return [
        {
            "ref": f"ref{i}",
            "score": 1.0 - i * 0.01,
            "content": SECRET_TEXT,
            "layer": "drawer",
            "rrf": 0.5,
            "arms": {"semantic": i, "bm25": i + 1},
        }
        for i in range(n)
    ]


class TestSearchEmitsRetrieved:
    def test_search_emits_memory_retrieved_with_expected_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, str, dict]] = []

        def fake_call(method: str, **kw: Any) -> Any:
            assert method == "search"
            return {"results": _search_hits(3)}

        def fake_emit(
            hook, event, *, ok=True, preview=None, data=None, session_id=None
        ):
            captured.append((hook, event, data or {}))

        monkeypatch.setattr(tm, "_call_client", fake_call)
        monkeypatch.setattr(tm, "emit_event", fake_emit)

        tool = MemoryTool()
        result = _run(
            tool.execute({"operation": "search", "query": "hello", "wing": "wing_x"})
        )
        assert result.success is True

        retrieved = [c for c in captured if c[1] == "retrieved"]
        assert len(retrieved) == 1
        _hook, _event, data = retrieved[0]
        assert data["source"] == "tool"
        assert data["op"] == "search"
        assert data["query"] == "hello"
        assert data["wing"] == "wing_x"
        assert isinstance(data["latency_ms"], float)
        assert len(data["hits"]) == 3
        for i, hit in enumerate(data["hits"], start=1):
            assert hit["ref"] == f"ref{i - 1}"
            assert hit["rank"] == i
            assert hit["layer"] == "drawer"
            assert "rrf" in hit
            assert "arms" in hit

    def test_no_hit_content_ever_appears_in_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []

        monkeypatch.setattr(
            tm, "_call_client", lambda method, **kw: {"results": _search_hits(5)}
        )
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append(data or {})
            ),
        )

        tool = MemoryTool()
        _run(tool.execute({"operation": "search", "query": "q"}))

        retrieved = [d for d in captured if d.get("op") == "search"]
        assert retrieved
        payload_str = str(retrieved[0])
        assert SECRET_TEXT not in payload_str
        assert "content" not in payload_str

    def test_query_is_redacted_before_emission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(tm, "_call_client", lambda method, **kw: {"results": []})
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append(data or {})
            ),
        )

        tool = MemoryTool()
        _run(tool.execute({"operation": "search", "query": SECRET_QUERY}))

        assert captured
        assert "sk-ant-" not in captured[0]["query"]
        assert "REDACTED" in captured[0]["query"]

    def test_hits_capped_at_20(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            tm, "_call_client", lambda method, **kw: {"results": _search_hits(37)}
        )
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append(data or {})
            ),
        )

        tool = MemoryTool()
        _run(tool.execute({"operation": "search", "query": "q"}))

        assert len(captured[0]["hits"]) == 20

    def test_emit_events_false_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_calls: list[Any] = []
        bridge_calls: list[Any] = []
        monkeypatch.setattr(
            tm, "_call_client", lambda method, **kw: {"results": _search_hits(2)}
        )
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda *a, **k: emit_calls.append((a, k)),
        )

        tool = MemoryTool(
            bridge_emit=lambda event, payload: bridge_calls.append((event, payload)),
            emit_events=False,
        )
        result = _run(tool.execute({"operation": "search", "query": "q"}))

        assert result.success is True
        assert emit_calls == []
        assert bridge_calls == []

    def test_emission_failure_never_breaks_the_operation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tm, "_call_client", lambda method, **kw: {"results": _search_hits(1)}
        )

        def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("observability backend down")

        monkeypatch.setattr(tm, "emit_event", _boom)

        def _boom_bridge(event: str, payload: Any) -> None:
            raise RuntimeError("bridge down")

        tool = MemoryTool(bridge_emit=_boom_bridge)
        result = _run(tool.execute({"operation": "search", "query": "q"}))

        assert result.success is True


class TestFactsAndIndexEmitRetrieved:
    def test_facts_op_emits_retrieved_with_fact_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []
        rows = [{"ref": "fact1", "text": SECRET_TEXT, "proof_count": 2}]
        monkeypatch.setattr(tm, "_call_client", lambda method, **kw: rows)
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append(data or {})
            ),
        )

        tool = MemoryTool()
        result = _run(tool.execute({"operation": "facts", "query": "q"}))

        assert result.success is True
        assert captured[0]["op"] == "facts"
        assert captured[0]["hits"][0]["layer"] == "fact"
        assert SECRET_TEXT not in str(captured[0])

    def test_index_op_emits_retrieved_with_index_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []
        rows = [{"scope": "room:decisions", "abstract": "secret abstract text"}]
        monkeypatch.setattr(tm, "_call_client", lambda method, **kw: rows)
        monkeypatch.setattr(
            tm,
            "emit_event",
            lambda hook, event, *, ok=True, preview=None, data=None, session_id=None: (
                captured.append(data or {})
            ),
        )

        tool = MemoryTool()
        result = _run(tool.execute({"operation": "index", "wing": "wing_x"}))

        assert result.success is True
        assert captured[0]["op"] == "index"
        assert captured[0]["hits"][0]["ref"] == "room:decisions"
        assert captured[0]["hits"][0]["layer"] == "index"
        assert "secret abstract text" not in str(captured[0])


# ---------------------------------------------------------------------------
# Shared helper unit tests (event_emitter.build_retrieved_data / summarize_hits)
# ---------------------------------------------------------------------------


class TestSharedHelpers:
    def test_summarize_hits_never_copies_content_fields(self) -> None:
        hits = [{"ref": "r1", "content": "secret", "text": "also secret", "score": 0.9}]
        summarized = summarize_hits(hits)
        assert summarized == [{"ref": "r1", "rank": 1, "score": 0.9}]

    def test_summarize_hits_caps_at_max_hits(self) -> None:
        hits = [{"ref": str(i)} for i in range(50)]
        assert len(summarize_hits(hits, max_hits=20)) == 20

    def test_build_retrieved_data_redacts_query(self) -> None:
        data = build_retrieved_data(
            source="tool",
            op="search",
            query=SECRET_QUERY,
            wing=None,
            hits=[],
            latency_ms=1.0,
        )
        assert "sk-ant-" not in data["query"]
