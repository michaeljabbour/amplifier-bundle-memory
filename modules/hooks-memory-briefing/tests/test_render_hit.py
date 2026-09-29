"""T6.3 -- date-stamped, passage-aware evidence rendering.

Covers the pure ``_render_hit``/``_date_stamp`` helpers (dates, facts,
passages, missing dates), the layered "Relevant evidence" section's use of
passage granularity, and the ``_call_client`` capability fallback (retry
without ``granularity`` on ``TypeError``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_briefing as briefing_mod
import pytest
from amplifier_module_hooks_memory_briefing import (
    MemoryBriefingHook,
    _date_stamp,
    _render_hit,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _date_stamp / _render_hit -- pure unit tests
# ---------------------------------------------------------------------------


class TestDateStamp:
    def test_prefers_observed_at_over_filed_at(self) -> None:
        hit = {
            "filed_at": "2026-01-01T00:00:00+00:00",
            "observed_at": "2020-05-05T00:00:00+00:00",
        }
        assert _date_stamp(hit) == "2020-05-05"

    def test_falls_back_to_filed_at(self) -> None:
        hit = {"filed_at": "2026-09-28T12:00:00+00:00"}
        assert _date_stamp(hit) == "2026-09-28"

    def test_missing_both_returns_empty(self) -> None:
        assert _date_stamp({}) == ""

    def test_malformed_value_returns_empty(self) -> None:
        assert _date_stamp({"filed_at": "not-a-date"}) == ""


class TestRenderHit:
    def test_plain_hit_with_date(self) -> None:
        hit = {"content": "some evidence", "filed_at": "2026-09-28T00:00:00+00:00"}
        assert _render_hit(hit) == "[2026-09-28] some evidence"

    def test_plain_hit_without_date(self) -> None:
        hit = {"content": "some evidence"}
        assert _render_hit(hit) == "some evidence"

    def test_fact_hit_renders_proof_count(self) -> None:
        hit = {
            "text": "the repo uses uv",
            "layer": "fact",
            "proof_count": 2,
            "filed_at": "2026-01-01T00:00:00+00:00",
        }
        assert _render_hit(hit) == "[2026-01-01] (fact, proof 2) the repo uses uv"

    def test_fact_hit_without_date_has_no_stamp(self) -> None:
        hit = {"text": "the repo uses uv", "layer": "fact", "proof_count": 2}
        assert _render_hit(hit) == "(fact, proof 2) the repo uses uv"

    def test_fact_hit_missing_proof_count_defaults_to_zero(self) -> None:
        hit = {"text": "x", "layer": "fact"}
        assert _render_hit(hit) == "(fact, proof 0) x"

    def test_passage_hit_gets_provenance_tag_by_default(self) -> None:
        hit = {
            "content": "a passage snippet",
            "layer": "passage",
            "drawer_ref": "abcdef1234567890",
        }
        assert _render_hit(hit) == "a passage snippet (from abcdef12)"

    def test_passage_hit_provenance_tag_can_be_disabled(self) -> None:
        hit = {
            "content": "a passage snippet",
            "layer": "passage",
            "drawer_ref": "abcdef1234567890",
        }
        assert _render_hit(hit, provenance_tag=False) == "a passage snippet"

    def test_passage_hit_without_drawer_ref_has_no_tag(self) -> None:
        hit = {"content": "a passage snippet", "layer": "passage"}
        assert _render_hit(hit) == "a passage snippet"

    def test_passage_hit_with_date_and_tag(self) -> None:
        hit = {
            "content": "a passage snippet",
            "layer": "passage",
            "drawer_ref": "abcdef1234567890",
            "observed_at": "2026-03-15T00:00:00+00:00",
        }
        assert _render_hit(hit) == "[2026-03-15] a passage snippet (from abcdef12)"

    def test_max_chars_truncates_content_not_prefix(self) -> None:
        hit = {"content": "x" * 500, "filed_at": "2026-09-28T00:00:00+00:00"}
        rendered = _render_hit(hit, max_chars=10)
        assert rendered == "[2026-09-28] " + "x" * 10

    def test_content_falls_back_to_text_key(self) -> None:
        hit = {"text": "from text key"}
        assert _render_hit(hit) == "from text key"


# ---------------------------------------------------------------------------
# _call_client capability fallback (real function, fake ensure_daemon)
# ---------------------------------------------------------------------------


class _ClientNoGranularity:
    """Shaped like a pre-T6.1 store: ``search`` has no granularity param."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, *, query: str, k: int, wing: str | None = None, **_: Any) -> Any:
        self.calls.append({"query": query, "k": k, "wing": wing})
        return {"results": [{"ref": "d1", "content": "fallback result"}]}


class _ClientWithGranularity:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        *,
        query: str,
        k: int,
        wing: str | None = None,
        granularity: str | None = None,
    ) -> Any:
        self.calls.append(
            {"query": query, "k": k, "wing": wing, "granularity": granularity}
        )
        return {
            "results": [{"ref": "d1", "content": "passage result", "layer": "passage"}]
        }


class TestCallClientCapabilityFallback:
    def test_retries_without_granularity_on_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ClientNoGranularity()
        monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: client)

        result = briefing_mod._call_client(
            "search", query="q", k=5, wing="w", granularity="passage"
        )

        assert result == {"results": [{"ref": "d1", "content": "fallback result"}]}
        assert len(client.calls) == 1  # the retry call, without granularity

    def test_passes_granularity_when_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ClientWithGranularity()
        monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: client)

        result = briefing_mod._call_client(
            "search", query="q", k=5, wing="w", granularity="passage"
        )

        assert result is not None
        assert client.calls[0]["granularity"] == "passage"

    def test_other_type_errors_still_surface_as_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TypeError unrelated to ``granularity`` must not be silently
        retried into an infinite loop -- it's caught by the outer handler
        exactly like any other failure."""

        class _Client:
            def search(self, **kwargs: Any) -> Any:
                raise TypeError("completely unrelated bug")

        monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: _Client())
        result = briefing_mod._call_client("search", query="q", k=5)
        assert result is None


# ---------------------------------------------------------------------------
# Layered "Relevant evidence" section requests passage granularity, and
# falls back cleanly when the daemon doesn't support it.
# ---------------------------------------------------------------------------


class TestEvidenceSectionPassageGranularity:
    def test_evidence_uses_render_hit_with_date_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)

        class _Client:
            def index(self, **kw: Any) -> Any:  # capability probe target
                return None

        responses = {
            "index": [],
            "standing": [],
            "facts": [],
            "search": {
                "results": [
                    {
                        "ref": "passage1",
                        "layer": "passage",
                        "drawer_ref": "drawer12345678",
                        "content": "a dated passage",
                        "filed_at": "2026-09-28T00:00:00+00:00",
                    }
                ]
            },
        }

        def fake_call_client(method: str, **kwargs: Any) -> Any:
            if method == "search":
                assert kwargs.get("granularity") == "passage"
            return responses.get(method)

        monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: _Client())
        monkeypatch.setattr(briefing_mod, "_call_client", fake_call_client)

        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        text = result.context_injection
        assert "[2026-09-28] a dated passage (from drawer12)" in text

    def test_evidence_falls_back_when_daemon_rejects_granularity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a real (fake) client whose ``search`` has no
        ``granularity`` parameter must still deliver evidence -- the
        _call_client-level retry, not a mocked ``_call_client``."""
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)

        class _OldStyleClient:
            def index(self, **kw: Any) -> Any:
                return []

            def standing(self, **kw: Any) -> Any:
                return []

            def facts(self, **kw: Any) -> Any:
                return []

            def search(
                self, *, query: str, k: int, wing: str | None = None, layers=None
            ):
                return {"results": [{"ref": "d1", "content": "plain drawer evidence"}]}

        monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: _OldStyleClient())

        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        assert "plain drawer evidence" in result.context_injection
