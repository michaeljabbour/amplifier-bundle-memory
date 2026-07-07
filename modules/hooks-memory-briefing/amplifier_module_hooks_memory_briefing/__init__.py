"""
amplifier-module-hooks-memory-briefing

Amplifier hook that fires at session:start and injects a concise
wake-up briefing into the context. The briefing is assembled from:
  1. Semantic search results for the opening prompt (memory drawers)
     -> importance-reranked before truncating to top 5 (CP4)
  2. Knowledge graph facts for the active project entity
  3. Recent agent diary entries
  4. project-context Tier 1 coordination files (HANDOFF.md,
     PROJECT_CONTEXT.md, GLOSSARY.md) -- if present in the project root

The briefing is ephemeral -- it orients the agent without persisting
in conversation history.

Re-ranking formula (CP4):
  final = semantic_score + weight * (importance - 0.5) * 0.08

Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): every
memory read (search / KG / diary) routes through MemoryClient via
ensure_daemon() against the auto-started memory daemon. There is no
vendor subprocess anywhere in this module.

Credits: project-context (github.com/michaeljabbour/project-context).
"""

from __future__ import annotations

import os
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


# Native cutover: the ONE transport seam for every memory read this
# hook performs. Hard dependency (amplifier-module-tool-memory already
# hard-depends on amplifier-data + fastembed, \u00a78) -- no defensive
# ImportError fallback; a missing import means the environment is genuinely
# misconfigured, not something a private duplicate helper should paper over.
from amplifier_module_tool_memory.client import ensure_daemon

try:
    # T1-MEM-3: bounded, saturating usage boost for the reranker.
    from amplifier_module_tool_memory.usage import (
        usage_adjustment as _usage_adjustment,
    )
except ImportError:

    def _usage_adjustment(  # type: ignore[misc]
        retrieval_count: int | None, *, weight: float = 1.0, saturation: float = 10.0
    ) -> float:
        return 0.0


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


# -- Re-ranking -------------------------------------------------------------

#: Scaling constant. Bounds max boost/penalty to +/-0.04 at weight=1.0.
_RERANK_SCALE = 0.08

#: Default importance when no ``has_importance`` KG fact is present.
#: At 0.5 the boost is exactly zero -- preserves v1.1.0 ranking on untagged stores.
_DEFAULT_IMPORTANCE = 0.5


def _rerank_by_importance(
    results: list[dict[str, Any]],
    importance_lookup: dict[str, float],
    weight: float,
    usage_lookup: dict[str, int] | None = None,
    usage_weight: float = 0.0,
) -> list[dict[str, Any]]:
    """Re-rank search results using the importance signal (+ optional usage).

    Formula::

        final = semantic_score
                + weight       * (importance - 0.5) * 0.08
                + usage_weight * usage_adjustment(retrieval_count)   # T1-MEM-3

    Args:
        results:           Raw search results (each a dict with at least ``score``
                           and optionally ``id``).
        importance_lookup: Map from drawer-id -> importance float. Missing ids
                           default to 0.5 (zero boost -- safe for untagged stores).
        weight:            Multiplier on the importance signal. 0.0 = disabled
                           (pure semantic, identical to v1.1.0).
        usage_lookup:      T1-MEM-3. Optional map drawer-id -> retrieval_count,
                           sourced from amplifier-data's access-count fold (NOT
                           re-implemented here). Missing ids contribute nothing.
        usage_weight:      Multiplier on the usage term. Default 0.0 = disabled,
                           so this is a pure no-op vs prior behaviour and the R@5
                           recall guarantee is preserved unless explicitly enabled.

    Returns:
        Results sorted by ``final`` descending. Original list is not modified.
        When ``weight == 0.0`` and ``usage_weight == 0.0`` the sort is stable and
        order matches raw semantic (all boosts are 0.0).

    This function is pure -- no daemon calls, no side effects. Pass pre-built
    lookup dicts so tests can inject fixed values.
    """
    if not results:
        return []

    usage_on = usage_weight > 0.0 and bool(usage_lookup)

    if weight == 0.0 and not usage_on:  # exact: config-parsed float -- safe for ==
        # Fast path: zero boost for every result. Stable sort by semantic score
        # (descending) -- identical to v1.1.0.
        return sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)

    ulookup = usage_lookup or {}

    def _final(r: dict[str, Any]) -> float:
        sem = r.get("score", 0.0)
        rid = r.get("id", "")
        imp = importance_lookup.get(rid, _DEFAULT_IMPORTANCE)
        score = sem + weight * (imp - _DEFAULT_IMPORTANCE) * _RERANK_SCALE
        if usage_on:
            score += _usage_adjustment(ulookup.get(rid, 0), weight=usage_weight)
        return score

    # Python's sort is stable: equal finals preserve original relative order.
    return sorted(results, key=_final, reverse=True)


# -- Native transport seam ---------------------------------------------------


def _call_client(method: str, **kwargs: Any) -> Any:
    """Invoke ``MemoryClient.<method>(**kwargs)`` against the native memory
    daemon. Native cutover: replaces the old vendor subprocess
    transport this hook used exclusively.

    The briefing is a best-effort, session-start convenience -- it must
    never prevent a session from starting, so any failure (daemon
    unavailable, or a genuine call error) returns ``None`` (callers already
    treat that as "nothing to show for this section" and skip it) but IS
    observed via ``emit_event``, unlike a silently-swallowed subprocess
    failure.
    """
    try:
        client = ensure_daemon()
        if client is None:
            raise RuntimeError("memory daemon unavailable")
        return getattr(client, method)(**kwargs)
    except Exception as exc:
        try:
            emit_event(
                "memory-briefing",
                "mcp_call_failed",
                ok=False,
                data={"tool": method, "reason": str(exc)[:200]},
            )
        except Exception:
            pass
        return None


# -- KG importance lookup -----------------------------------------------------


def _query_importance(drawer_id: str) -> float:
    """Look up the ``has_importance`` fact for a drawer via the native generic
    ``query_facts`` tool.

    This is a DIRECT ref lookup, not an anchor-based KG entity query: a
    drawer's ``has_importance`` fact is asserted on the drawer's own
    content-addressed ref by ``NativeMemoryStore.file()``, not on a
    synthetic ``drawer:{id}`` entity (that anchor convention is reserved for
    KG facts filed via ``kg_add``, e.g. garden's cluster edges). The fact's
    object is itself a cell ref (the value bytes), so a second call
    regenerates it -- mirrors ``NativeMemoryStore._first_fact_value``.

    Returns the float importance value, or ``_DEFAULT_IMPORTANCE`` (0.5)
    if the fact is absent, the calls fail, or the value cannot be parsed.
    """
    try:
        client = ensure_daemon()
        if client is None:
            return _DEFAULT_IMPORTANCE
        result = client.query_facts(subject=drawer_id, predicate="has_importance")
        if result.success and result.output:
            cell = client.regenerate(result.output[0].object)
            return float(cell.payload.decode("utf-8"))
    except Exception:
        pass
    return _DEFAULT_IMPORTANCE


def _build_importance_lookup(
    results: list[dict[str, Any]],
) -> dict[str, float]:
    """Query importance for all results; N sequential native calls.

    TODO: Batch into a single query if the daemon gains bulk-fact-lookup
    support (tracked in future optimization). Each call is a fast local HTTP
    round trip to the auto-started daemon (not a subprocess spawn), so the
    added latency at 8 results is small -- acceptable for session:start.
    """
    return {r["id"]: _query_importance(r["id"]) for r in results if "id" in r}


# -- Helpers -------------------------------------------------------------------


def _detect_project_name() -> str:
    """Detect the active project name from git remote or cwd."""
    try:
        import subprocess

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
        # Trim to budget -- rough 4 chars/token estimate
        max_chars = budget * 4
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\u2026(truncated)"
        section = f"{header}\n{content}"
        sections.append(section)
        budget -= len(section) // 4

    if not sections:
        return ""
    return "### Coordination Files\n\n" + "\n\n---\n\n".join(sections)


# -- Briefing assembly ----------------------------------------------------------


def _build_briefing(
    project: str,
    opening_query: str,
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    include_project_context: bool,
    importance_weight: float = 1.0,
) -> tuple[str, list[str], int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Assemble a concise briefing from memory search, KG, diary, and coordination files.

    Returns (briefing_text, sections, token_estimate, results_fetched, results_after_rerank).

    CP4 change:
    - Fetches ``limit=8`` candidates (up from 5) for rerank headroom.
    - Looks up importance per result via a native fact lookup.
    - Re-ranks by ``final = semantic + weight*(importance-0.5)*0.08``.
    - Truncates to top 5 after re-ranking.
    - If ``importance_weight == 0.0``, skips the importance lookups entirely (fast path).
    """
    sections: list[str] = []
    approx_tokens = 0
    results_fetched: list = []
    results_after_rerank: list = []

    # 1. Semantic search -- fetch extra candidates for re-ranking headroom (CP4: 8 -> top 5)
    wing = f"wing_{project}"
    search_result = (
        _call_client(
            "search",
            query=opening_query or f"recent work on {project}",
            k=8,  # CP4: increased from 5 for rerank headroom
            wing=wing,
        )
        or {}
    )
    raw_hits = (
        search_result.get("results", []) if isinstance(search_result, dict) else []
    )
    # Native search returns {ref, score, content, wing, room, category, source} --
    # map onto the id/text/room shape the rerank + rendering code below uses.
    raw_results = [
        {
            "id": str(h.get("ref", "")),
            "score": float(h.get("score", 0.0) or 0.0),
            "room": h.get("room"),
            "text": h.get("content", "") or "",
        }
        for h in raw_hits
        if isinstance(h, dict)
    ]
    results_fetched = list(raw_results)

    # 2. Importance re-ranking (CP4)
    if importance_weight == 0.0 or not raw_results:
        # Fast path: disabled or nothing to rank.
        # weight=0 mathematically equals zero boost -- identical to v1.1.0 top-5.
        results = raw_results[:5]
    else:
        # Build importance lookup via N native fact lookups, then re-rank.
        lookup = _build_importance_lookup(raw_results)
        reranked = _rerank_by_importance(raw_results, lookup, weight=importance_weight)
        results = reranked[:5]

    results_after_rerank = list(results)

    if results:
        lines = [f"**Recent memories -- `{project}`:**"]
        for r in results:
            room = r.get("room", "") or ""
            text = r.get("text", "").strip()[:300]
            lines.append(f"- [{room}] {text}")
        section = "\n".join(lines)
        sections.append(section)
        approx_tokens += len(section) // 4

    # 3. Knowledge graph facts (entity-level, not drawer-level -- unchanged)
    if include_kg and approx_tokens < token_budget:
        facts_raw = _call_client("kg_query", subject=project, predicate=None) or []
        facts = [
            {"subject": s, "predicate": p, "object": o, "current": True}
            for s, p, o in facts_raw
        ]
        if facts:
            lines = [f"**Knowledge graph -- `{project}`:**"]
            for fact in facts[:8]:
                subj = fact.get("subject", "")
                pred = fact.get("predicate", "")
                obj = fact.get("object", "")
                current = "\u2713" if fact.get("current") else "\u2717"
                lines.append(f"- {current} {subj} {pred} {obj}")
            section = "\n".join(lines)
            sections.append(section)
            approx_tokens += len(section) // 4

    # 4. Agent diary
    if include_diary and approx_tokens < token_budget:
        diary_entries = (
            _call_client("diary_read", agent_name="amplifier", last_n=3) or []
        )
        # Native diary_read returns {ref, entry, topic, seq_pos} -- no wall-clock
        # date is tracked natively (SeqPos ordering only); label with seq_pos
        # rather than fabricate a date (honest degradation, mirrors garden's
        # lookback_days limitation).
        entries = [
            {"date": f"#{e.get('seq_pos', '?')}", "content": e.get("entry", "")}
            for e in diary_entries
            if isinstance(e, dict)
        ]
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

    header = f"## Memory Briefing -- `{project}`\n"
    footer = (
        "\n*This briefing is ephemeral and will not appear in conversation history.*"
    )
    briefing = header + "\n\n".join(sections) + footer
    return briefing, sections, approx_tokens, results_fetched, results_after_rerank


# -- Hook class --------------------------------------------------------------


class MemoryBriefingHook:
    name = "hooks-memory-briefing"
    events = ["session:start"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        bridge_emit: AsyncBridge | None = None,
    ) -> None:
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

        self._bridge_emit: AsyncBridge = bridge_emit or NOOP_ASYNC_BRIDGE

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        sid = data.get("session_id")

        # Check if the memory daemon is reachable/spawnable -- skip the
        # memory-derived sections silently if not (native cutover: replaces
        # the old vendor CLI version probe).
        if ensure_daemon() is None:
            if self.emit_events:
                emit_event(
                    "memory-briefing",
                    "briefing_skipped",
                    ok=False,
                    data={"reason": "daemon_unavailable"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory:briefing_skipped",
                        {"ok": False, "reason": "daemon_unavailable"},
                    )
                except Exception:
                    pass
            # Still inject project-context coordination files even without the daemon
            if self.include_project_context:
                pc_dir = _find_project_context_dir()
                if pc_dir:
                    section = _read_coordination_files(pc_dir, self.token_budget)
                    if section:
                        header = "## Session Briefing (coordination files only)\n"
                        footer = "\n*Memory daemon not available -- semantic search skipped.*"
                        return HookResult(
                            action="inject_context",
                            context_injection=header + section + footer,
                            context_injection_role="user",
                            ephemeral=self.ephemeral,
                            suppress_output=True,
                        )
            return HookResult(action="continue")

        project = _detect_project_name()
        opening_query = data.get("opening_prompt", "") or data.get("prompt", "")

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

        # Derive drawer_ids from results_after_rerank (list of dicts)
        drawer_ids = [
            r["id"]
            for r in (results_after_rerank or [])
            if isinstance(r, dict) and "id" in r
        ]

        if briefing:
            if self.emit_events:
                emit_event(
                    "memory-briefing",
                    "briefing_assembled",
                    ok=True,
                    preview=None,
                    data={
                        "project": project,
                        "section_count": len(sections),
                        "token_estimate": token_estimate,
                        "results_fetched": len(results_fetched or []),
                        "results_after_rerank": len(results_after_rerank or []),
                        "importance_weight": self.briefing_importance_weight,
                    },
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory:briefing_assembled",
                        {
                            "ok": True,
                            "project": project,
                            "section_count": len(sections),
                            "token_estimate": token_estimate,
                            "drawer_ids": drawer_ids,
                            "importance_weight": self.briefing_importance_weight,
                        },
                    )
                except Exception:
                    pass
            return HookResult(
                action="inject_context",
                context_injection=briefing,
                context_injection_role="user",
                ephemeral=self.ephemeral,
                suppress_output=True,
            )

        if self.emit_events:
            emit_event(
                "memory-briefing",
                "briefing_skipped",
                ok=False,
                data={"reason": "no_content"},
                session_id=sid,
            )
            try:
                await self._bridge_emit(
                    "memory:briefing_skipped",
                    {"ok": False, "reason": "no_content", "project": project},
                )
            except Exception:
                pass
        return HookResult(action="continue")


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the memory-briefing hook into the Amplifier coordinator."""
    register_events(
        coordinator,
        "memory-briefing",
        ["memory:briefing_assembled", "memory:briefing_skipped"],
    )

    bridge_emit = make_async_bridge(coordinator)

    hook = MemoryBriefingHook(config, bridge_emit=bridge_emit)
    for event in hook.events:
        coordinator.hooks.register(event, hook, name=hook.name)
    return {
        "name": "hooks-memory-briefing",
        "version": "1.1.0",
        "provides": ["memory-briefing"],
    }
