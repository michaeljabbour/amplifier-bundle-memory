"""
Where the coordination files live, and what the session:start hook says about
them.

Two behaviours are covered:

1. **Location.** ``.amplifier/project-context/`` is the home; a legacy
   top-level ``project-context/`` is still found so unmigrated repos keep
   working; ``context_dir`` overrides both.
2. **Inventory.** The injected block names the files that actually exist.
   Without it, agents follow the generic instructions and open Tier 2 files
   that were never created — 66 failed reads in one measured week.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from amplifier_module_hooks_project_context import (
    ProjectContextStartHook,
    _find_project_context_dir,
    _inventory_line,
    _present_files,
    _scaffold_project_context,
    _scaffold_target,
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git-rooted working directory that the resolver will stop at."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_tier1(pc_dir: Path) -> None:
    pc_dir.mkdir(parents=True, exist_ok=True)
    (pc_dir / "HANDOFF.md").write_text("# Handoff\n\nlast session\n")
    (pc_dir / "PROJECT_CONTEXT.md").write_text("# Project Context\n\nphase: x\n")


class TestLocation:
    def test_prefers_hidden_dir(self, repo: Path) -> None:
        hidden = repo / ".amplifier" / "project-context"
        hidden.mkdir(parents=True)
        (repo / "project-context").mkdir()
        assert _find_project_context_dir() == hidden

    def test_finds_legacy_top_level(self, repo: Path) -> None:
        legacy = repo / "project-context"
        legacy.mkdir()
        assert _find_project_context_dir() == legacy

    def test_none_when_absent(self, repo: Path) -> None:
        assert _find_project_context_dir() is None

    def test_walks_up_to_git_root(self, repo: Path) -> None:
        hidden = repo / ".amplifier" / "project-context"
        hidden.mkdir(parents=True)
        nested = repo / "modules" / "deep"
        nested.mkdir(parents=True)
        os.chdir(nested)
        assert _find_project_context_dir() == hidden

    def test_stops_at_git_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coordination dir above an unrelated repo must not leak into it."""
        (tmp_path / ".amplifier" / "project-context").mkdir(parents=True)
        inner = tmp_path / "other-repo"
        (inner / ".git").mkdir(parents=True)
        monkeypatch.chdir(inner)
        assert _find_project_context_dir() is None

    def test_relative_context_dir_overrides(self, repo: Path) -> None:
        (repo / ".amplifier" / "project-context").mkdir(parents=True)
        custom = repo / "docs" / "coord"
        custom.mkdir(parents=True)
        assert _find_project_context_dir("docs/coord") == custom

    def test_absolute_context_dir_short_circuits(
        self, repo: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path.parent / "elsewhere-coord"
        elsewhere.mkdir(exist_ok=True)
        assert _find_project_context_dir(str(elsewhere)) == elsewhere
        assert _find_project_context_dir(str(elsewhere / "nope")) is None


class TestScaffold:
    def test_scaffolds_into_hidden_dir(self, repo: Path) -> None:
        pc_dir, created = _scaffold_project_context(repo)
        assert pc_dir == repo / ".amplifier" / "project-context"
        assert (pc_dir / "HANDOFF.md").is_file()
        assert any(name.endswith("AGENTS.md") for name in created)

    def test_scaffold_target_honors_config(self, repo: Path) -> None:
        assert _scaffold_target(repo, "docs/coord") == repo / "docs" / "coord"
        assert _scaffold_target(repo, "/tmp/abs-coord") == Path("/tmp/abs-coord")

    def test_agents_md_names_the_real_directory(self, repo: Path) -> None:
        _scaffold_project_context(repo)
        agents = (repo / "AGENTS.md").read_text()
        assert ".amplifier/project-context/HANDOFF.md" in agents
        # The old template told agents to *read* Tier 2 files that scaffolding
        # never creates; the new one tells them to write.
        assert "don't hunt for them" in agents


class TestInventory:
    def test_lists_only_present_files(self, repo: Path) -> None:
        pc_dir = repo / ".amplifier" / "project-context"
        _write_tier1(pc_dir)
        assert _present_files(pc_dir) == ["PROJECT_CONTEXT.md", "HANDOFF.md"]

        line = _inventory_line(pc_dir)
        assert "HANDOFF.md" in line
        assert "WAYSOFWORKING.md" not in line
        assert "does not exist yet" in line

    def test_handles_empty_directory(self, repo: Path) -> None:
        pc_dir = repo / ".amplifier" / "project-context"
        pc_dir.mkdir(parents=True)
        assert "(none yet)" in _inventory_line(pc_dir)


class TestStartHook:
    @pytest.mark.asyncio
    async def test_injects_inventory_with_tier1(self, repo: Path) -> None:
        _write_tier1(repo / ".amplifier" / "project-context")
        hook = ProjectContextStartHook(
            {"setup_if_missing": False, "emit_events": False}
        )
        result = await hook("session:start", {"session_id": "s1"})
        assert result.action == "inject_context"
        block = result.context_injection
        assert "files present: PROJECT_CONTEXT.md, HANDOFF.md" in block
        assert "last session" in block

    @pytest.mark.asyncio
    async def test_continues_when_no_directory(self, repo: Path) -> None:
        hook = ProjectContextStartHook(
            {"setup_if_missing": False, "emit_events": False}
        )
        result = await hook("session:start", {"session_id": "s1"})
        assert result.action == "continue"
