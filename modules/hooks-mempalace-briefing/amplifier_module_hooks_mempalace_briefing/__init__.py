"""
amplifier-module-hooks-mempalace-briefing

Amplifier hook that fires at session:start and injects a concise
wake-up briefing into the context. The briefing is assembled from:
  1. Semantic search results for the opening prompt (palace drawers)
  2. Knowledge graph facts for the active project entity
  3. Recent agent diary entries
  4. project-context Tier 1 coordination files (HANDOFF.md,
     PROJECT_CONTEXT.md, GLOSSARY.md) — if present in the project root

The briefing is ephemeral — it orients the agent without persisting
in conversation history.

Credits: MemPalace (github.com/MemPalace/mempalace),
         project-context (github.com/michaeljabbour/project-context).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from amplifier_core import Hook, HookContext, mount_hook  # type: ignore


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mcp_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a MemPalace MCP tool via the CLI."""
    payload = json.dumps({"tool": tool_name, "arguments": args})
    try:
        result = subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def _detect_project_name() -> str:
    """Detect the active project name from git remote or cwd."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url.rstrip("/").split("/")[-1].replace(".git", "")
    except Exception:
        pass
    return Path(os.getcwd()).name


def _find_project_context_dir() -> Path | None:
    """Walk up from cwd to find a project-context/ directory."""
    cwd = Path(os.getcwd())
    for candidate in [cwd, *cwd.parents]:
        pc = candidate / "project-context"
        if pc.is_dir():
            return pc
        # Stop at git root
        if (candidate / ".git").exists():
            break
    return None


def _read_coordination_files(pc_dir: Path, token_budget_remaining: int) -> str:
    """Read Tier 1 project-context files and return a formatted section."""
    sections: list[str] = []
    budget = token_budget_remaining

    # Priority order: HANDOFF (most recent session state) > PROJECT_CONTEXT > GLOSSARY
    tier1 = [
        ("HANDOFF.md", "**Last session handoff:**"),
        ("PROJECT_CONTEXT.md", "**Project context:**"),
        ("GLOSSARY.md", "**Glossary (active terms):**"),
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
        # Trim to budget — rough 4 chars/token estimate
        max_chars = budget * 4
        if len(content) > max_chars:
            content = content[:max_chars] + "\n…(truncated)"
        section = f"{header}\n{content}"
        sections.append(section)
        budget -= len(section) // 4

    if not sections:
        return ""
    return "### Coordination Files\n\n" + "\n\n---\n\n".join(sections)


# ── Briefing assembly ──────────────────────────────────────────────────────────

def _build_briefing(
    project: str,
    opening_query: str,
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    include_project_context: bool,
) -> str:
    """Assemble a concise briefing from palace search, KG, diary, and coordination files."""
    sections: list[str] = []
    approx_tokens = 0

    # 1. Semantic search — recent work in the active wing
    wing = f"wing_{project}"
    search_result = _mcp_call("mempalace_search", {
        "query": opening_query or f"recent work on {project}",
        "wing": wing,
        "limit": 5,
    })
    results = search_result.get("results", [])
    if results:
        lines = [f"**Recent palace memories — `{project}`:**"]
        for r in results:
            room = r.get("room", "")
            text = r.get("text", "").strip()[:300]
            lines.append(f"- [{room}] {text}")
        section = "\n".join(lines)
        sections.append(section)
        approx_tokens += len(section) // 4

    # 2. Knowledge graph facts
    if include_kg and approx_tokens < token_budget:
        kg_result = _mcp_call("mempalace_kg_query", {"entity": project})
        facts = kg_result.get("facts", [])
        if facts:
            lines = [f"**Knowledge graph — `{project}`:**"]
            for fact in facts[:8]:
                subj = fact.get("subject", "")
                pred = fact.get("predicate", "")
                obj = fact.get("object", "")
                current = "✓" if fact.get("current") else "✗"
                lines.append(f"- {current} {subj} {pred} {obj}")
            section = "\n".join(lines)
            sections.append(section)
            approx_tokens += len(section) // 4

    # 3. Agent diary
    if include_diary and approx_tokens < token_budget:
        diary_result = _mcp_call("mempalace_diary_read", {
            "agent_name": "amplifier",
            "last_n": 3,
        })
        entries = diary_result.get("entries", [])
        if entries:
            lines = ["**Recent agent diary:**"]
            for e in entries:
                date = e.get("date", "")
                content = e.get("content", "").strip()[:200]
                lines.append(f"- [{date}] {content}")
            section = "\n".join(lines)
            sections.append(section)
            approx_tokens += len(section) // 4

    # 4. project-context Tier 1 coordination files
    if include_project_context and approx_tokens < token_budget:
        pc_dir = _find_project_context_dir()
        if pc_dir:
            remaining_budget = token_budget - approx_tokens
            coord_section = _read_coordination_files(pc_dir, remaining_budget)
            if coord_section:
                sections.append(coord_section)
                approx_tokens += len(coord_section) // 4

    if not sections:
        return ""

    header = f"## Memory Briefing — `{project}`\n"
    footer = "\n*This briefing is ephemeral and will not appear in conversation history.*"
    return header + "\n\n".join(sections) + footer


# ── Hook class ─────────────────────────────────────────────────────────────────

class MempalaceBriefingHook(Hook):
    name = "hooks-mempalace-briefing"
    events = ["session:start"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.token_budget: int = self.config.get("token_budget", 1500)
        self.include_kg: bool = self.config.get("include_kg", True)
        self.include_diary: bool = self.config.get("include_diary", True)
        self.include_project_context: bool = self.config.get("include_project_context", True)
        self.ephemeral: bool = self.config.get("ephemeral", True)

    async def handle(self, ctx: HookContext) -> None:  # type: ignore[override]
        # Check if mempalace is available — skip silently if not installed
        try:
            subprocess.run(["mempalace", "--version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Still inject project-context coordination files even without MemPalace
            if self.include_project_context:
                pc_dir = _find_project_context_dir()
                if pc_dir:
                    section = _read_coordination_files(pc_dir, self.token_budget)
                    if section:
                        header = "## Session Briefing (coordination files only)\n"
                        footer = "\n*MemPalace not available — semantic search skipped.*"
                        ctx.inject_context(header + section + footer, ephemeral=self.ephemeral)
            return

        project = _detect_project_name()
        opening_query = ctx.event.get("opening_prompt", "")

        briefing = _build_briefing(
            project=project,
            opening_query=opening_query,
            token_budget=self.token_budget,
            include_kg=self.include_kg,
            include_diary=self.include_diary,
            include_project_context=self.include_project_context,
        )

        if briefing:
            ctx.inject_context(briefing, ephemeral=self.ephemeral)


def mount() -> list[Hook]:
    """Amplifier module entry point."""
    return [MempalaceBriefingHook()]
