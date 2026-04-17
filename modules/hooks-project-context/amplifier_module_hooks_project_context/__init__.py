"""
amplifier-module-hooks-project-context

Amplifier hook that integrates the project-context coordination file system.

At session:start:
  - Locates project-context/ by walking up from cwd to the git root
  - If tier1_always=True, reads PROJECT_CONTEXT.md, GLOSSARY.md, HANDOFF.md
    and injects them as ephemeral context
  - If setup_if_missing=True and no project-context/ exists, scaffolds the
    directory with template stubs so the Curator can populate them

At session:end:
  - If handoff_on_end=True, delegates to the Curator agent to update
    HANDOFF.md, PROVENANCE.md, GLOSSARY.md, and WAYSOFWORKING.md

Credits: project-context (github.com/michaeljabbour/project-context).
"""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

try:
    from amplifier_core import Hook, HookContext  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests)
    class Hook:  # type: ignore
        name: str = ""
        events: list[str] = []

        def __init__(self, config: dict[str, Any] | None = None) -> None:
            pass

    class HookContext:  # type: ignore
        def __init__(self, event: dict[str, Any] | None = None) -> None:
            self.event: dict[str, Any] = event or {}
            self.session_id: str | None = None

        def inject_context(self, content: str, *, ephemeral: bool = True) -> None:
            pass

        def delegate_to_agent(self, agent: str, *, prompt: str) -> None:
            pass


try:
    from amplifier_module_tool_mempalace.event_emitter import emit_event
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


# ── Template stubs ────────────────────────────────────────────────────────────

_AGENTS_MD = """\
# Agent Instructions

This project uses a coordination file system in `project-context/`.
These files give you persistent memory across sessions. **Read them before starting any work.**

## Starting a Session

Read these files in order:
1. `project-context/PROJECT_CONTEXT.md` — current project state, phase, team
2. `project-context/GLOSSARY.md` — terminology (use these terms exactly)
3. `project-context/HANDOFF.md` — what happened last session, what to do next

Also read when relevant:
- `project-context/STRUCTURE.md` — before creating or moving files
- `project-context/WAYSOFWORKING.md` — for workflows, failure patterns, verification steps
- `project-context/PROVENANCE.md` — to understand why a decision was made
- `project-context/EXPERIMENT_JOURNAL.md` — to see what was tried and learned

## Ending a Session

Update `project-context/HANDOFF.md` with:
- What you accomplished (specific files, decisions, results)
- What's blocked or unresolved
- What the next session should start with
- Non-obvious context the next agent needs

## Continuous Improvement

| When you... | Update |
|-------------|--------|
| Use a term not in the glossary | `project-context/GLOSSARY.md` |
| Make a design or architecture decision | `project-context/PROVENANCE.md` |
| Hit an error and find the fix | `project-context/WAYSOFWORKING.md` |
| Create or move files | `project-context/STRUCTURE.md` |
| Run an experiment or benchmark | `project-context/EXPERIMENT_JOURNAL.md` |
| Change the project phase or milestone | `project-context/PROJECT_CONTEXT.md` |
| Finish any session | `project-context/HANDOFF.md` |
"""

_PROJECT_CONTEXT_STUB = """\
# Project Context

<!-- Update: current phase, milestone, team, active work -->

## Current State

**Phase:** [e.g., "Initial setup", "Feature development", "Stabilization"]
**Milestone:** [e.g., "v1.0.0 release"]
**Active work:** [What is being built right now]

## Team

| Person | Role |
|--------|------|
| [Name] | [Role] |

## Recent Milestones

- [Date] — [Milestone]
"""

_GLOSSARY_STUB = """\
# Glossary

<!-- Add terms as they emerge. Format: Term | Means | Does NOT Mean -->

| Term | Means | Does NOT Mean |
|------|-------|---------------|
| [Term] | [Definition] | [Common misuse] |
"""

_HANDOFF_STUB = f"""\
# Handoff

*Last updated: {date.today().isoformat()} — initial scaffold*

## Accomplished

- Project coordination files scaffolded by hooks-project-context.

## Blocked / Unresolved

- Nothing yet.

## Start Here Next Session

- Review PROJECT_CONTEXT.md and update the current phase and active work.

## Non-Obvious Context

- This project uses the project-context coordination system. Keep these files
  accurate as you work — they are the human-readable memory layer.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _find_git_root() -> Path | None:
    """Find the git root from cwd."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _find_project_context_dir() -> Path | None:
    """Walk up from cwd to find a project-context/ directory."""
    cwd = Path(os.getcwd())
    for candidate in [cwd, *cwd.parents]:
        pc = candidate / "project-context"
        if pc.is_dir():
            return pc
        if (candidate / ".git").exists():
            break
    return None


def _scaffold_project_context(git_root: Path) -> tuple[Path, list[str]]:
    """Scaffold a minimal project-context/ directory at the git root.

    Returns (pc_dir, list_of_created_files).
    """
    pc_dir = git_root / "project-context"
    pc_dir.mkdir(exist_ok=True)

    stubs = {
        "PROJECT_CONTEXT.md": _PROJECT_CONTEXT_STUB,
        "GLOSSARY.md": _GLOSSARY_STUB,
        "HANDOFF.md": _HANDOFF_STUB,
    }
    files_created: list[str] = []
    for filename, content in stubs.items():
        path = pc_dir / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            files_created.append(str(path))

    # Write AGENTS.md at the git root if not present
    agents_path = git_root / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(_AGENTS_MD, encoding="utf-8")
        files_created.append(str(agents_path))

    return pc_dir, files_created


def _read_tier1(pc_dir: Path, token_budget: int) -> tuple[str, list[str], int]:
    """Read Tier 1 coordination files and return (content, files_read, token_estimate)."""
    sections: list[str] = []
    budget = token_budget
    files_read: list[str] = []

    tier1 = [
        ("HANDOFF.md", "### Last Session Handoff"),
        ("PROJECT_CONTEXT.md", "### Project Context"),
        ("GLOSSARY.md", "### Glossary"),
    ]

    for filename, header in tier1:
        if budget <= 0:
            break
        path = pc_dir / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        max_chars = budget * 4
        if len(content) > max_chars:
            content = content[:max_chars] + "\n…(truncated)"
        section = f"{header}\n\n{content}"
        sections.append(section)
        files_read.append(str(path))
        budget -= len(section) // 4

    if not sections:
        return "", [], 0

    result = "## Project Coordination Files\n\n" + "\n\n---\n\n".join(sections)
    token_estimate = len(result) // 4
    return result, files_read, token_estimate


# ── Hook classes ─────────────────────────────────────────────────────────────


class ProjectContextStartHook(Hook):
    name = "hooks-project-context-start"
    events = ["session:start"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.tier1_always: bool = self.config.get("tier1_always", True)
        self.setup_if_missing: bool = self.config.get("setup_if_missing", True)
        self.token_budget: int = self.config.get("token_budget", 800)
        self.emit_events: bool = bool(self.config.get("emit_events", True))

    async def handle(self, ctx: HookContext) -> None:  # type: ignore[override]
        sid = getattr(ctx, "session_id", None) or ctx.event.get("session_id")

        pc_dir = _find_project_context_dir()

        if pc_dir is None and self.setup_if_missing:
            git_root = _find_git_root()
            if git_root:
                pc_dir, files_created = _scaffold_project_context(git_root)
                if self.emit_events and files_created:
                    emit_event(
                        "project-context",
                        "coordination_scaffolded",
                        ok=True,
                        data={
                            "pc_dir": str(pc_dir),
                            "files_created": files_created,
                        },
                        session_id=sid,
                    )

        if pc_dir is None:
            return

        if self.tier1_always:
            block, files_read, token_estimate = _read_tier1(pc_dir, self.token_budget)
            if block:
                ctx.inject_context(block, ephemeral=True)
                if self.emit_events:
                    emit_event(
                        "project-context",
                        "coordination_read",
                        ok=True,
                        data={
                            "files_read": files_read,
                            "token_estimate": token_estimate,
                        },
                        session_id=sid,
                    )


class ProjectContextEndHook(Hook):
    name = "hooks-project-context-end"
    events = ["session:end"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.handoff_on_end: bool = self.config.get("handoff_on_end", True)
        self.emit_events: bool = bool(self.config.get("emit_events", True))

    async def handle(self, ctx: HookContext) -> None:  # type: ignore[override]
        if not self.handoff_on_end:
            return

        pc_dir = _find_project_context_dir()
        if pc_dir is None:
            return

        sid = getattr(ctx, "session_id", None) or ctx.event.get("session_id")

        # Delegate to the Curator agent to update coordination files.
        # The Curator has the full session context and knows what to write.
        prompt = (
            "Update the project-context coordination files for this session. "
            "Rewrite HANDOFF.md with what was accomplished, what is blocked, "
            "and what the next session should start with. "
            "Append to PROVENANCE.md, GLOSSARY.md, and WAYSOFWORKING.md "
            "if any decisions, terms, or patterns emerged."
        )
        ctx.delegate_to_agent(
            "mempalace:curator",
            prompt=prompt,
        )

        if self.emit_events:
            emit_event(
                "project-context",
                "curator_delegated",
                ok=True,
                data={"prompt_preview": prompt[:200]},
                session_id=sid,
            )


def mount() -> list[Hook]:
    """Amplifier module entry point."""
    return [ProjectContextStartHook(), ProjectContextEndHook()]
