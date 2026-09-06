"""
amplifier-module-hooks-project-context

Amplifier hook that integrates the project-context coordination file system.

At session:start:
  - Locates the coordination directory by walking up from cwd to the git root,
    preferring `.amplifier/project-context/` and falling back to a legacy
    top-level `project-context/` (see "Location" below)
  - If tier1_always=True, reads PROJECT_CONTEXT.md, GLOSSARY.md, HANDOFF.md
    and injects them as ephemeral context, prefixed with an inventory of the
    files that actually exist
  - If setup_if_missing=True and no coordination directory exists, scaffolds
    one with template stubs so the Curator can populate them

Location:
  The coordination files live in `.amplifier/project-context/` — the same
  hidden directory the rest of Amplifier already uses for per-repo state, and
  the target of the `@project:` mention shortcut. A top-level
  `project-context/` is still found when present, so existing repos keep
  working; `context_dir` in config overrides both (absolute or relative to the
  repo root).

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
    from amplifier_core import HookResult  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests)
    class HookResult:  # type: ignore
        def __init__(self, *, action: str = "continue", **kwargs: Any) -> None:
            self.action = action
            for k, v in kwargs.items():
                setattr(self, k, v)


try:
    from amplifier_module_tool_memory.event_emitter import emit_event
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


try:
    from amplifier_module_tool_memory.coordinator_bridge import (
        NOOP_ASYNC_BRIDGE,
        AsyncBridge,
        make_async_bridge,
        register_events,
    )
except ImportError:
    AsyncBridge = Any  # type: ignore

    async def NOOP_ASYNC_BRIDGE(event: str, payload: Any) -> None:  # type: ignore[misc]
        pass

    def make_async_bridge(coordinator: Any) -> Any:  # type: ignore[misc]
        return NOOP_ASYNC_BRIDGE

    def register_events(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


# ── Template stubs ─────────────────────────────────────────────────────────────

_AGENTS_MD_TEMPLATE = """\
# Agent Instructions

This project uses a coordination file system in `{pc}/`.
These files give you persistent memory across sessions.

## Starting a Session

The session briefing injects the Tier 1 files automatically and lists which
coordination files exist — you do not need to open them yourself, and you
should not go looking for ones the inventory doesn't name.

| Tier 1 (injected every session) | |
|---|---|
| `{pc}/PROJECT_CONTEXT.md` | current project state, phase, team |
| `{pc}/GLOSSARY.md` | terminology (use these terms exactly) |
| `{pc}/HANDOFF.md` | what happened last session, what to do next |

## Tier 2 — write these, don't hunt for them

These are created **when a session has something to put in them**. A missing
file means nobody has needed it yet; create it then, rather than reading it now.

| When you... | Write to |
|-------------|--------|
| Use a term not in the glossary | `{pc}/GLOSSARY.md` |
| Make a design or architecture decision | `{pc}/PROVENANCE.md` |
| Hit an error and find the fix | `{pc}/WAYSOFWORKING.md` |
| Create or move files | `{pc}/STRUCTURE.md` |
| Run an experiment or benchmark | `{pc}/EXPERIMENT_JOURNAL.md` |
| Change the project phase or milestone | `{pc}/PROJECT_CONTEXT.md` |
| Finish any session | `{pc}/HANDOFF.md` |

## Ending a Session

Update `{pc}/HANDOFF.md` with:
- What you accomplished (specific files, decisions, results)
- What's blocked or unresolved
- What the next session should start with
- Non-obvious context the next agent needs
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


# ── Helpers ────────────────────────────────────────────────────────────────────


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


# The preferred home: the hidden per-repo directory Amplifier already owns and
# the `@project:` mention shortcut already points at. The bare name is the
# legacy location, still discovered so existing repos keep working.
#
# NOTE: hooks-memory-briefing carries an identical resolver. The duplication is
# deliberate: sibling tool-memory imports must be ImportError-guarded per
# BUNDLE_GUIDE, and a guarded import whose fallback resolves *differently* is
# worse than two copies that resolve the same.
HIDDEN_DIR = Path(".amplifier") / "project-context"
LEGACY_DIR = Path("project-context")

# Files the Curator maintains, in the order an inventory should list them.
KNOWN_FILES = (
    "PROJECT_CONTEXT.md",
    "GLOSSARY.md",
    "HANDOFF.md",
    "STRUCTURE.md",
    "WAYSOFWORKING.md",
    "PROVENANCE.md",
    "EXPERIMENT_JOURNAL.md",
)


def _candidate_dirs(context_dir: str | None) -> tuple[Path, ...]:
    """Relative directory names to probe at each level of the upward walk."""
    if context_dir:
        return (Path(context_dir).expanduser(),)
    return (HIDDEN_DIR, LEGACY_DIR)


def _find_project_context_dir(context_dir: str | None = None) -> Path | None:
    """Walk up from cwd to find the coordination directory.

    Probes ``.amplifier/project-context/`` before the legacy top-level
    ``project-context/`` at each level, so a repo that has migrated wins over a
    stale copy higher up the tree. An absolute ``context_dir`` short-circuits
    the walk entirely.
    """
    candidates = _candidate_dirs(context_dir)
    if len(candidates) == 1 and candidates[0].is_absolute():
        return candidates[0] if candidates[0].is_dir() else None

    cwd = Path(os.getcwd())
    for parent in [cwd, *cwd.parents]:
        for name in candidates:
            pc = parent / name
            if pc.is_dir():
                return pc
        if (parent / ".git").exists():
            break
    return None


def _scaffold_target(git_root: Path, context_dir: str | None = None) -> Path:
    """Where a new coordination directory should be created."""
    if context_dir:
        configured = Path(context_dir).expanduser()
        return configured if configured.is_absolute() else git_root / configured
    return git_root / HIDDEN_DIR


def _present_files(pc_dir: Path) -> list[str]:
    """Names of the coordination files that actually exist, in known order."""
    present = [name for name in KNOWN_FILES if (pc_dir / name).is_file()]
    extra = sorted(
        p.name for p in pc_dir.glob("*.md") if p.is_file() and p.name not in KNOWN_FILES
    )
    return present + extra


def _scaffold_project_context(
    git_root: Path, context_dir: str | None = None
) -> tuple[Path, list[str]]:
    """Scaffold a minimal coordination directory for the repo.

    Returns (pc_dir, list_of_created_files).
    """
    pc_dir = _scaffold_target(git_root, context_dir)
    pc_dir.mkdir(parents=True, exist_ok=True)

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

    # Write AGENTS.md at the git root if not present, naming the directory we
    # actually created rather than a hardcoded one.
    agents_path = git_root / "AGENTS.md"
    if not agents_path.exists():
        try:
            rel = pc_dir.relative_to(git_root).as_posix()
        except ValueError:
            rel = str(pc_dir)
        agents_path.write_text(_AGENTS_MD_TEMPLATE.format(pc=rel), encoding="utf-8")
        files_created.append(str(agents_path))

    return pc_dir, files_created


def _inventory_line(pc_dir: Path) -> str:
    """One line naming the directory and the files that exist inside it.

    Costs ~30 tokens and removes an entire class of wasted tool calls: without
    it, agents follow the generic instructions and open STRUCTURE.md /
    WAYSOFWORKING.md / PROVENANCE.md in repos that never created them.
    """
    present = _present_files(pc_dir)
    listed = ", ".join(present) if present else "(none yet)"
    return (
        f"Location: `{pc_dir}` — files present: {listed}. "
        "Any coordination file not listed does not exist yet: create it when "
        "you have something to record, rather than trying to read it."
    )


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

    result = (
        "## Project Coordination Files\n\n"
        + _inventory_line(pc_dir)
        + "\n\n"
        + "\n\n---\n\n".join(sections)
    )
    token_estimate = len(result) // 4
    return result, files_read, token_estimate


# ── Hook classes ───────────────────────────────────────────────────────────────


class ProjectContextStartHook:
    name = "hooks-project-context-start"
    events = ["session:start"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        bridge_emit: AsyncBridge | None = None,
    ) -> None:
        self.config = config or {}
        self.tier1_always: bool = self.config.get("tier1_always", True)
        self.setup_if_missing: bool = self.config.get("setup_if_missing", True)
        self.token_budget: int = self.config.get("token_budget", 800)
        self.context_dir: str | None = self.config.get("context_dir")
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        self._bridge_emit: AsyncBridge = bridge_emit or NOOP_ASYNC_BRIDGE

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        sid = data.get("session_id")

        pc_dir = _find_project_context_dir(self.context_dir)

        if pc_dir is None and self.setup_if_missing:
            git_root = _find_git_root()
            if git_root:
                pc_dir, files_created = _scaffold_project_context(
                    git_root, self.context_dir
                )
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
                    try:
                        await self._bridge_emit(
                            "memory:coordination_scaffolded",
                            {
                                "ok": True,
                                "pc_dir": str(pc_dir),
                                "files_created": files_created,
                            },
                        )
                    except Exception:
                        pass

        if pc_dir is None:
            return HookResult(action="continue")

        if self.tier1_always:
            block, files_read, token_estimate = _read_tier1(pc_dir, self.token_budget)
            if block:
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
                    try:
                        await self._bridge_emit(
                            "memory:coordination_read",
                            {
                                "ok": True,
                                "files_read": files_read,
                                "token_estimate": token_estimate,
                            },
                        )
                    except Exception:
                        pass
                return HookResult(
                    action="inject_context",
                    context_injection=block,
                    context_injection_role="user",
                    ephemeral=True,
                    suppress_output=True,
                )

        return HookResult(action="continue")


class ProjectContextEndHook:
    name = "hooks-project-context-end"
    events = ["session:end"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        bridge_emit: AsyncBridge | None = None,
    ) -> None:
        self.config = config or {}
        self.handoff_on_end: bool = self.config.get("handoff_on_end", True)
        self.context_dir: str | None = self.config.get("context_dir")
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        self._bridge_emit: AsyncBridge = bridge_emit or NOOP_ASYNC_BRIDGE

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        if not self.handoff_on_end:
            return HookResult(action="continue")

        pc_dir = _find_project_context_dir(self.context_dir)
        if pc_dir is None:
            return HookResult(action="continue")

        sid = data.get("session_id")

        # Agent delegation is not yet available in the HookResult API.
        # For now, just emit the event so external watchers know the session
        # ended with a pending handoff. The Curator can be invoked manually.
        prompt = (
            f"Update the coordination files in {pc_dir} for this session. "
            "Rewrite HANDOFF.md with what was accomplished, what is blocked, "
            "and what the next session should start with. "
            "Append to PROVENANCE.md, GLOSSARY.md, and WAYSOFWORKING.md "
            "if any decisions, terms, or patterns emerged — creating them if "
            "they do not exist yet."
        )
        if self.emit_events:
            emit_event(
                "project-context",
                "curator_handoff_requested",
                ok=True,
                data={"prompt_preview": prompt[:200]},
                session_id=sid,
            )
            try:
                await self._bridge_emit(
                    "memory:curator_handoff_requested",
                    {
                        "ok": True,
                        "prompt_preview": prompt[:200],
                    },
                )
            except Exception:
                pass
        return HookResult(action="continue")


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the project-context hooks into the Amplifier coordinator."""

    register_events(
        coordinator,
        "memory-project-context",
        [
            "memory:coordination_read",
            "memory:coordination_scaffolded",
            "memory:curator_handoff_requested",
        ],
    )

    bridge_emit = make_async_bridge(coordinator)

    start_hook = ProjectContextStartHook(config, bridge_emit=bridge_emit)
    end_hook = ProjectContextEndHook(config, bridge_emit=bridge_emit)
    for hook in (start_hook, end_hook):
        for evt in hook.events:
            coordinator.hooks.register(evt, hook, name=hook.name)
    return {
        "name": "hooks-project-context",
        "version": "1.2.0",
        "provides": ["project-context-start", "project-context-end"],
    }
