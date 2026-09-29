"""T3.2 (D19) -- layered briefing: capability probe (index/legacy fallback),
section order/content, token-budget respect, marker line unchanged, and the
legacy path pinned byte-identical to pre-P3 behavior.

All daemon calls are faked via monkeypatching ``_call_client``/``ensure_daemon``
on the hook module -- no real memory daemon.
"""

from __future__ import annotations

import asyncio
from typing import Any

import amplifier_module_hooks_memory_briefing as briefing_mod
import pytest
from amplifier_module_hooks_memory_briefing import MemoryBriefingHook


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeClientWithIndex:
    """Any attribute named ``index`` makes ``hasattr(client, "index")`` true."""

    def index(self, **kw: Any) -> Any:  # pragma: no cover - never actually called
        return None


class _FakeClientWithoutIndex:
    """A client shaped like an older tool-memory pin (no L3 ops)."""


def _install_fake_daemon(
    monkeypatch: pytest.MonkeyPatch, client: Any, responses: dict[str, Any]
) -> None:
    monkeypatch.setattr(briefing_mod, "ensure_daemon", lambda: client)

    def fake_call_client(method: str, **kwargs: Any) -> Any:
        return responses.get(method)

    monkeypatch.setattr(briefing_mod, "_call_client", fake_call_client)


class TestCapabilityProbe:
    def test_layered_mode_with_index_capable_client_stays_layered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [
                    {
                        "scope": "room:r",
                        "abstract": "room summary",
                        "pending_changes": 0,
                    }
                ],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        assert result.action == "inject_context"
        assert "Memory map" in result.context_injection
        assert result.context_injection.startswith("## Memory Briefing")

    def test_layered_mode_falls_back_to_legacy_without_index_capability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithoutIndex(),
            {
                "search": {"results": []},
                "kg_query": [],
                "diary_read": [],
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        # No content anywhere -> continue (legacy path with all-empty responses).
        assert result.action == "continue"


class TestLayeredSectionsOrderAndContent:
    def test_sections_appear_in_order_with_stale_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [
                    {
                        "scope": "room:fresh",
                        "abstract": "fresh room abstract",
                        "pending_changes": 0,
                    },
                    {
                        "scope": "room:stale",
                        "abstract": "stale room abstract",
                        "pending_changes": 999,
                    },
                ],
                "standing": [
                    {
                        "question_ref": "q1",
                        "question": "what is our package manager",
                        "answer": "uv",
                        "stale": False,
                    },
                    {
                        "question_ref": "q2",
                        "question": "what is our test runner",
                        "answer": None,
                        "stale": True,
                    },
                ],
                "facts": [
                    {"text": "the repo uses uv", "proof_count": 2},
                ],
                "search": {
                    "results": [
                        {"ref": "d1", "room": "r", "content": "some evidence text"}
                    ]
                },
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        text = result.context_injection

        map_idx = text.index("Memory map")
        standing_idx = text.index("Standing answers")
        facts_idx = text.index("Known facts")
        evidence_idx = text.index("Relevant evidence")
        assert map_idx < standing_idx < facts_idx < evidence_idx

        assert "stale room abstract (stale)" in text
        assert "fresh room abstract" in text
        assert "fresh room abstract (stale)" not in text
        assert "Q: what is our package manager A: uv" in text
        assert "stale, needs refresh" in text
        assert "what is our test runner" in text
        assert "(fact, proof 2) the repo uses uv" in text
        assert "some evidence text" in text

    def test_budget_limits_later_sections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        big_abstract = "x" * 2000
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [
                    {
                        "scope": "room:r",
                        "abstract": big_abstract,
                        "pending_changes": 0,
                    }
                ],
                "standing": [
                    {
                        "question_ref": "q1",
                        "question": "should not appear",
                        "answer": "nope",
                        "stale": False,
                    }
                ],
                "facts": [{"text": "should not appear either", "proof_count": 1}],
                "search": {"results": [{"ref": "d1", "room": "r", "content": "x"}]},
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        # Tiny budget: only the first section (Memory map) fits before the
        # budget gate closes off the rest.
        hook = MemoryBriefingHook(
            {"briefing_mode": "layered", "emit_events": False, "token_budget": 10}
        )
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        text = result.context_injection
        assert "Memory map" in text
        assert "should not appear" not in text
        assert "should not appear either" not in text


class TestLegacyPinned:
    def test_legacy_mode_matches_pre_p3_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),  # capable client, but config forces legacy
            {
                "search": {
                    "results": [
                        {"ref": "d1", "room": "r", "content": "legacy evidence"}
                    ]
                },
                "kg_query": [],
                "diary_read": [],
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        hook = MemoryBriefingHook(
            {
                "briefing_mode": "legacy",
                "emit_events": False,
                "briefing_importance_weight": 0.0,
            }
        )
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        text = result.context_injection
        assert text.startswith("## Memory Briefing")
        assert "Recent memories" in text
        assert "Memory map" not in text
        assert "legacy evidence" in text


class TestCoordinationFilesSection:
    def test_coordination_section_first_in_layered_and_identical_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        (pc_dir / "HANDOFF.md").write_text(
            "Last session accomplished X, Y, Z. Next: do W.", encoding="utf-8"
        )
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: pc_dir)
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")

        # -- Legacy --
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithoutIndex(),
            {
                "search": {"results": []},
                "kg_query": [],
                "diary_read": [],
            },
        )
        legacy_hook = MemoryBriefingHook(
            {
                "briefing_mode": "legacy",
                "emit_events": False,
                "briefing_importance_weight": 0.0,
            }
        )
        legacy_result = _run(
            legacy_hook("session:start", {"session_id": "s1", "prompt": ""})
        )
        legacy_text = legacy_result.context_injection
        assert "### Coordination Files" in legacy_text
        assert "Last session accomplished X, Y, Z" in legacy_text

        # Extract the exact coordination section substring from legacy output
        # (from its header to end-of-briefing footer) to compare byte-for-byte.
        coord_start = legacy_text.index("### Coordination Files")
        legacy_coord_section = legacy_text[coord_start:].split(
            "\n*Injected from memory for orientation"
        )[0]

        # -- Layered --
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [{"scope": "room:r", "abstract": "abc", "pending_changes": 0}],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        layered_hook = MemoryBriefingHook(
            {"briefing_mode": "layered", "emit_events": False}
        )
        layered_result = _run(
            layered_hook("session:start", {"session_id": "s1", "prompt": ""})
        )
        layered_text = layered_result.context_injection

        assert "### Coordination Files" in layered_text
        assert "Last session accomplished X, Y, Z" in layered_text

        # Identical content to legacy's rendering.
        layered_coord_start = layered_text.index("### Coordination Files")
        layered_coord_section = layered_text[layered_coord_start:].split(
            "\n\n**Memory map"
        )[0]
        assert layered_coord_section == legacy_coord_section

        # First in layered mode: appears before every other section header.
        map_idx = layered_text.index("Memory map")
        assert layered_coord_start < map_idx
        # Right after the top header line, before anything else.
        header_end = layered_text.index("\n", layered_text.index("## Memory Briefing"))
        first_section_start = layered_text.index(
            "###", header_end
        )  # first "###"-prefixed section
        assert first_section_start == layered_coord_start

    def test_coordination_section_respects_include_project_context_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        pc_dir = tmp_path / "project-context"
        pc_dir.mkdir()
        (pc_dir / "HANDOFF.md").write_text("should not appear", encoding="utf-8")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: pc_dir)
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [{"scope": "room:r", "abstract": "abc", "pending_changes": 0}],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        hook = MemoryBriefingHook(
            {
                "briefing_mode": "layered",
                "emit_events": False,
                "include_project_context": False,
            }
        )
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        assert "should not appear" not in result.context_injection
        assert "### Coordination Files" not in result.context_injection


class TestReflectHookMarkerCompatibility:
    def test_layered_header_still_matches_reflect_hooks_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hooks-memory-reflect skips messages starting with '## Memory
        Briefing' -- verify the layered header keeps that exact prefix."""
        _install_fake_daemon(
            monkeypatch,
            _FakeClientWithIndex(),
            {
                "index": [{"scope": "room:r", "abstract": "abc", "pending_changes": 0}],
                "standing": [],
                "facts": [],
                "search": {"results": []},
            },
        )
        monkeypatch.setattr(briefing_mod, "_detect_project_name", lambda: "proj")
        monkeypatch.setattr(briefing_mod, "_find_project_context_dir", lambda: None)
        hook = MemoryBriefingHook({"briefing_mode": "layered", "emit_events": False})
        result = _run(hook("session:start", {"session_id": "s1", "prompt": ""}))
        assert result.context_injection.lstrip().startswith("## Memory Briefing")
