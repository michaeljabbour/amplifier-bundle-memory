"""
amplifier-module-hooks-mempalace-briefing

Amplifier hook that fires at session:start and injects a concise
wake-up briefing into the context. The briefing is assembled from:
  1. Semantic search results for the opening prompt (palace drawers)
     → importance-reranked before truncating to top 5 (CP4)
  2. Knowledge graph facts for the active project entity
  3. Recent agent diary entries
  4. project-context Tier 1 coordination files (HANDOFF.md,
     PROJECT_CONTEXT.md, GLOSSARY.md) — if present in the project root

The briefing is ephemeral — it orients the agent without persisting
in conversation history.

Re-ranking formula (CP4):
  final = semantic_score + weight * (importance - 0.5) * 0.08

Credits: MemPalace (github.com/MemPalace/mempalace),
         project-context (github.com/michaeljabbour/project-context).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    from amplifier_core import Hook, HookContext, mount_hook  # type: ignore
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

    def mount_hook(*args: Any, **kwargs: Any) -> None:  # type: ignore
        pass


try:
    from amplifier_module_tool_mempalace.event_emitter import emit_event
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


# ── Re-ranking ──────────────────────────────────────────────────────────────

#: Scaling constant. Bounds max boost/penalty to ±0.04 at weight=1.0.
_RERANK_SCALE = 0.08

#: Default importance when no ``has_importance`` KG fact is present.
#: At 0.5 the boost is exactly zero — preserves v1.1.0 ranking on untagged palaces.
_DEFAULT_IMPORTANCE = 0.5


def _rerank_by_importance(
    results: list[dict[str, Any]],
    importance_lookup: dict[str, float],
    weight: float,
) -> list[dict[str, Any]]:
    """Re-rank search results using the importance signal.

    Formula::

        final = semantic_score + weight * (importance - 0.5) * 0.08

    Args:
        results:           Raw search results (each a dict with at least ``score``
                           and optionally ``id``).
        importance_lookup: Map from drawer-id → importance float. Missing ids
                           default to 0.5 (zero boost — safe for untagged palaces).
        weight:            Multiplier on the importance signal. 0.0 = disabled
                           (pure semantic, identical to v1.1.0).

    Returns:
        Results sorted by ``final`` descending. Original list is not modified.
        When ``weight == 0.0`` the sort is stable and order matches raw semantic
        (because all boosts are 0.0).

    This function is pure — no MCP calls, no side effects. Pass a pre-built
    ``importance_lookup`` dict so tests can inject fixed values.
    """
    if not results:
        return []

    if weight == 0.0:  # exact: config-parsed float, not computed — safe for ==
        # Fast path: weight=0 means zero boost for every result.
        # Return a stable sort by semantic score (descending) — identical to v1.1.0.
        return sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)

    def _final(r: dict[str, Any]) -> float:
        sem = r.get("score", 0.0)
        imp = importance_lookup.get(r.get("id", ""), _DEFAULT_IMPORTANCE)
        return sem + weight * (imp - _DEFAULT_IMPORTANCE) * _RERANK_SCALE

    # Python's sort is stable: equal finals preserve original relative order.
    return sorted(results, key=_final, reverse=True)


# ── KG importance lookup ────────────────────────────────────────────────────


def _query_importance(drawer_id: str) -> float:
    """Look up the ``has_importance`` KG fact for a drawer via MCP.

    Returns the float importance value, or ``_DEFAULT_IMPORTANCE`` (0.5)
    if the fact is absent, the KG call fails, or the value cannot be parsed.
    """
    try:
        kg_result = _mcp_call("mempalace_kg_query", {"entity": f"drawer:{drawer_id}"})
        for fact in kg_result.get("facts", []):
            if fact.get("predicate") == "has_importance" and fact.get("current", True):
                return float(fact["object"])
    except Exception:
        pass
    return _DEFAULT_IMPORTANCE


def _build_importance_lookup(
    results: list[dict[str, Any]],
) -> dict[str, float]:
    """Query importance for all results; N sequential MCP calls.

    TODO: Batch into a single query if mempalace_kg_query gains prefix/wildcard
    support (tracked in future optimization). Each call is ~100ms; at 8 results
    this adds ~800ms to briefing assembly — acceptable for session:start.
    """
    return {r["id"]: _query_importance(r["id"]) for r in results if "id" in r}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _mcp_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a MemPalace MCP tool via the CLI."""
    payload = json.dumps({"tool": tool_name, "arguments": args})
    try:
        result = subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True,
            text=True,
            timeout=20,
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
            capture_output=True,
            text=True,
            timeout=5,
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


# ── Briefing assembly ────────────────────────────────────────────────────────


def _build_briefing(
    project: str,
    opening_query: str,
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    include_project_context: bool,
    importance_weight: float = 1.0,
) -> tuple[str, list[str], int, int, int]:
    """Assemble a concise briefing from palace search, KG, diary, and coordination files.

    Returns (briefing_text, sections, token_estimate, results_fetched, results_after_rerank).

    CP4 change:
    - Fetches ``limit=8`` candidates (up from 5) for rerank headroom.
    - Looks up importance per result via KG.
    - Re-ranks by ``final = semantic + weight*(importance-0.5)*0.08``.
    - Truncates to top 5 after re-ranking.
    - If ``importance_weight == 0.0``, skips KG lookups entirely (fast path).
    """
    sections: list[str] = []
    approx_tokens = 0
    results_fetched = 0
    results_after_rerank = 0

    # 1. Semantic search — fetch extra candidates for re-ranking headroom (CP4: 8 → top 5)
    wing = f"wing_{project}"
    search_result = _mcp_call(
        "mempalace_search",
        {
            "query": opening_query or f"recent work on {project}",
            "wing": wing,
            "limit": 8,  # CP4: increased from 5 for rerank headroom
        },
    )
    raw_results = search_result.get("results", [])
    results_fetched = len(raw_results)

    # 2. Importance re-ranking (CP4)
    if importance_weight == 0.0 or not raw_results:
        # Fast path: disabled or nothing to rank.
        # weight=0 mathematically equals zero boost — identical to v1.1.0 top-5.
        results = raw_results[:5]
    else:
        # Build importance lookup via N KG queries, then re-rank.
        lookup = _build_importance_lookup(raw_results)
        reranked = _rerank_by_importance(raw_results, lookup, weight=importance_weight)
        results = reranked[:5]

    results_after_rerank = len(results)

    if results:
        lines = [f"**Recent palace memories — `{project}`:**"]
        for r in results:
            room = r.get("room", "")
            text = r.get("text", "").strip()[:300]
            lines.append(f"- [{room}] {text}")
        section = "\n".join(lines)
        sections.append(section)
        approx_tokens += len(section) // 4

    # 3. Knowledge graph facts (entity-level, not drawer-level — unchanged)
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

    # 4. Agent diary
    if include_diary and approx_tokens < token_budget:
        diary_result = _mcp_call(
            "mempalace_diary_read",
            {
                "agent_name": "amplifier",
                "last_n": 3,
            },
        )
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

    # 5. project-context Tier 1 coordination files
    if include_project_context and approx_tokens < token_budget:
        pc_dir = _find_project_context_dir()
        if pc_dir:
            remaining_budget = token_budget - approx_tokens
            coord_section = _read_coordination_files(pc_dir, remaining_budget)
            if coord_section:
                sections.append(coord_section)
                approx_tokens += len(coord_section) // 4

    if not sections:
        return "", [], 0, results_fetched, results_after_rerank

    header = f"## Memory Briefing — `{project}`\n"
    footer = (
        "\n*This briefing is ephemeral and will not appear in conversation history.*"
    )
    briefing = header + "\n\n".join(sections) + footer
    return briefing, sections, approx_tokens, results_fetched, results_after_rerank


# ── Hook class ───────────────────────────────────────────────────────────────


class MempalaceBriefingHook(Hook):
    name = "hooks-mempalace-briefing"
    events = ["session:start"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.token_budget: int = self.config.get("token_budget", 1500)
        self.include_kg: bool = self.config.get("include_kg", True)
        self.include_diary: bool = self.config.get("include_diary", True)
        self.include_project_context: bool = self.config.get(
            "include_project_context", True
        )
        self.ephemeral: bool = self.config.get("ephemeral", True)
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        # CP4: importance re-ranking weight. 0.0 = disabled (exact v1.1.0 behavior).
        self.briefing_importance_weight: float = float(
            self.config.get("briefing_importance_weight", 1.0)
        )

    async def handle(self, ctx: HookContext) -> None:  # type: ignore[override]
        sid = getattr(ctx, "session_id", None) or ctx.event.get("session_id")

        # Check if mempalace is available — skip silently if not installed
        try:
            subprocess.run(["mempalace", "--version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            if self.emit_events:
                emit_event(
                    "mempalace-briefing",
                    "briefing_skipped",
                    ok=False,
                    data={"reason": "mempalace_unavailable"},
                    session_id=sid,
                )
            # Still inject project-context coordination files even without MemPalace
            if self.include_project_context:
                pc_dir = _find_project_context_dir()
                if pc_dir:
                    section = _read_coordination_files(pc_dir, self.token_budget)
                    if section:
                        header = "## Session Briefing (coordination files only)\n"
                        footer = (
                            "\n*MemPalace not available — semantic search skipped.*"
                        )
                        ctx.inject_context(
                            header + section + footer, ephemeral=self.ephemeral
                        )
            return

        project = _detect_project_name()
        opening_query = ctx.event.get("opening_prompt", "")

        briefing, sections, token_estimate, results_fetched, results_after_rerank = (
            _build_briefing(
                project=project,
                opening_query=opening_query,
                token_budget=self.token_budget,
                include_kg=self.include_kg,
                include_diary=self.include_diary,
                include_project_context=self.include_project_context,
                importance_weight=self.briefing_importance_weight,
            )
        )

        if briefing:
            ctx.inject_context(briefing, ephemeral=self.ephemeral)
            if self.emit_events:
                emit_event(
                    "mempalace-briefing",
                    "briefing_assembled",
                    ok=True,
                    preview=None,
                    data={
                        "project": project,
                        "section_count": len(sections),
                        "token_estimate": token_estimate,
                        "results_fetched": results_fetched,
                        "results_after_rerank": results_after_rerank,
                        "importance_weight": self.briefing_importance_weight,
                    },
                    session_id=sid,
                )
        else:
            if self.emit_events:
                emit_event(
                    "mempalace-briefing",
                    "briefing_skipped",
                    ok=False,
                    data={"reason": "no_content"},
                    session_id=sid,
                )


def mount() -> list[Hook]:
    """Amplifier module entry point."""
    return [MempalaceBriefingHook()]
