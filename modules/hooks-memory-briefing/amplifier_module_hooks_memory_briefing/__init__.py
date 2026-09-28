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

The briefing is injected with ``ephemeral=True``. Whether it then stays in
conversation history is the orchestrator's call: under loop-streaming's
default ``ephemeral_injection_mode: persist`` it is stored as a (protected)
message and rides along on every later request of the session.

Delivery (perf/startup-latency): amplifier-core DISCARDS the HookResult of
``session:start`` (the kernel emits it and ignores the return value), so an
injection returned there never reaches the model -- while the daemon
round-trips that built it blocked the first turn for seconds. The briefing is
therefore now prefetched on a background thread from ``mount()`` (before the
user has even typed, in interactive sessions) and delivered on the first
``prompt:submit`` -- whose ephemeral injections the orchestrator does honor --
with a short bounded wait (``deliver_wait_s``). If it is still not ready it is
delivered, non-blocking, at the next ``provider:request``/``prompt:submit``
once it is. The search query is unchanged (``session:start`` payloads never
carried the opening prompt, so the query was always
``"recent work on {project}"``), which is what makes the prefetch possible.
Results are cached per process (``cache_ttl_s``) so sibling sessions reuse
them; coordination files are re-read fresh at delivery.

Re-ranking formula (CP4):
  final = semantic_score + weight * (importance - 0.5) * 0.08

Native cutover (B2, the native-cutover design history): every
memory read (search / KG / diary) routes through MemoryClient via
ensure_daemon() against the auto-started memory daemon. There is no
vendor subprocess anywhere in this module.

Credits: project-context (github.com/michaeljabbour/project-context).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from dataclasses import dataclass, field
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
    from amplifier_module_tool_memory.event_emitter import (
        build_retrieved_data,
        emit_event,
    )
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass

    def build_retrieved_data(**kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
        return {}


# Native cutover: the ONE transport seam for every memory read this
# hook performs. Hard dependency (amplifier-module-tool-memory already
# hard-depends on amplifier-data + fastembed, §8) -- no defensive
# ImportError fallback; a missing import means the environment is genuinely
# misconfigured, not something a private duplicate helper should paper over.
from amplifier_module_tool_memory.automation_gate import automation_opt_out
from amplifier_module_tool_memory.client import ensure_daemon

#: P3 (T3.2, D19) -- shared with the store's own staleness gate purely for
#: display purposes (the "Memory map" section's "(stale)" marker). Reused
#: rather than duplicated: a single source of truth for the threshold.
try:
    from amplifier_module_tool_memory.store import (
        _INDEX_STALE_AFTER_DEFAULT,
    )
except ImportError:  # pragma: no cover - defensive, mirrors other imports here
    _INDEX_STALE_AFTER_DEFAULT = 10

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


#: Count of ``_call_client`` failures in this process. A prefetch compares it
#: before/after its lookups: any failure in between (possibly a sibling
#: prefetch's -- that only makes caching more conservative) marks the result
#: incomplete, so it is delivered but never reused from the cache.
_CALL_FAILURES = 0
_CALL_FAILURES_LOCK = threading.Lock()


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
    global _CALL_FAILURES
    try:
        client = ensure_daemon()
        if client is None:
            raise RuntimeError("memory daemon unavailable")
        return getattr(client, method)(**kwargs)
    except Exception as exc:
        with _CALL_FAILURES_LOCK:
            _CALL_FAILURES += 1
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


@dataclass
class _MemoryPart:
    """The daemon-derived half of a briefing (search + KG + diary sections).

    Split from coordination-file assembly so it can be fetched off the
    critical path (and cached per process) while HANDOFF.md & co. are still
    read fresh at delivery time.
    """

    sections: list[str] = field(default_factory=list)
    approx_tokens: int = 0
    results_fetched: list[dict[str, Any]] = field(default_factory=list)
    results_after_rerank: list[dict[str, Any]] = field(default_factory=list)


def _importance_from_hits(raw_results: list[dict[str, Any]]) -> dict[str, float] | None:
    """Importance lookup from the ``importance`` field search hits now carry.

    The daemon resolves each hit's ``has_importance`` fact inside the search's
    own log fold (same fact, same float parse as :func:`_query_importance`),
    which replaces 2 extra round-trips -- each a full log fold server-side --
    per hit. Returns ``None`` when any hit lacks the field (an older daemon),
    so the caller falls back to per-hit lookups. ``None`` values (fact absent
    or unparsable) map to the same 0.5 default ``_query_importance`` uses.
    """
    lookup: dict[str, float] = {}
    for r in raw_results:
        if "importance" not in r:
            return None
        imp = r["importance"]
        try:
            lookup[r["id"]] = float(imp) if imp is not None else _DEFAULT_IMPORTANCE
        except (TypeError, ValueError):
            lookup[r["id"]] = _DEFAULT_IMPORTANCE
    return lookup


def _search_section(
    project: str, opening_query: str, importance_weight: float
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Semantic search + importance rerank -> (section|None, fetched, after_rerank)."""
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
    # Native search returns {ref, score, content, wing, room, category, source,
    # importance} -- map onto the id/text/room shape the rerank + rendering
    # code below uses.
    raw_results = []
    for h in raw_hits:
        if not isinstance(h, dict):
            continue
        r = {
            "id": str(h.get("ref", "")),
            "score": float(h.get("score", 0.0) or 0.0),
            "room": h.get("room"),
            "text": h.get("content", "") or "",
        }
        if "importance" in h:
            r["importance"] = h["importance"]
        raw_results.append(r)
    results_fetched = list(raw_results)

    # 2. Importance re-ranking (CP4)
    if importance_weight == 0.0 or not raw_results:
        # Fast path: disabled or nothing to rank.
        # weight=0 mathematically equals zero boost -- identical to v1.1.0 top-5.
        results = raw_results[:5]
    else:
        lookup = _importance_from_hits(raw_results)
        if lookup is None:
            # Older daemon: N per-hit native fact lookups.
            lookup = _build_importance_lookup(raw_results)
        reranked = _rerank_by_importance(raw_results, lookup, weight=importance_weight)
        results = reranked[:5]

    results_after_rerank = list(results)
    if not results:
        return None, results_fetched, results_after_rerank
    lines = [f"**Recent memories -- `{project}`:**"]
    for r in results:
        room = r.get("room", "") or ""
        text = r.get("text", "").strip()[:300]
        lines.append(f"- [{room}] {text}")
    return "\n".join(lines), results_fetched, results_after_rerank


def _kg_section(project: str) -> str | None:
    # 3. Knowledge graph facts (entity-level, not drawer-level -- unchanged)
    facts_raw = _call_client("kg_query", subject=project, predicate=None) or []
    facts = [
        {"subject": s, "predicate": p, "object": o, "current": True}
        for s, p, o in facts_raw
    ]
    if not facts:
        return None
    lines = [f"**Knowledge graph -- `{project}`:**"]
    for fact in facts[:8]:
        subj = fact.get("subject", "")
        pred = fact.get("predicate", "")
        obj = fact.get("object", "")
        current = "✓" if fact.get("current") else "✗"
        lines.append(f"- {current} {subj} {pred} {obj}")
    return "\n".join(lines)


def _diary_section() -> str | None:
    # 4. Agent diary
    diary_entries = _call_client("diary_read", agent_name="amplifier", last_n=3) or []
    # Native diary_read returns {ref, entry, topic, seq_pos} -- no wall-clock
    # date is tracked natively (SeqPos ordering only); label with seq_pos
    # rather than fabricate a date (honest degradation, mirrors garden's
    # lookback_days limitation).
    entries = [
        {"date": f"#{e.get('seq_pos', '?')}", "content": e.get("entry", "")}
        for e in diary_entries
        if isinstance(e, dict)
    ]
    if not entries:
        return None
    lines = ["**Recent agent diary:**"]
    for e in entries:
        date = e.get("date", "")
        content = e.get("content", "").strip()[:200]
        lines.append(f"- [{date}] {content}")
    return "\n".join(lines)


def _fetch_memory_part(
    project: str,
    opening_query: str,
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    importance_weight: float = 1.0,
) -> _MemoryPart:
    """Fetch the search / KG / diary sections CONCURRENTLY.

    The three lookups are independent daemon calls, so they are issued in
    parallel rather than back-to-back. The original budget gating is applied
    afterwards, in the original order (search, then KG only if still under
    budget, then diary only if still under budget), so the assembled text is
    identical to the sequential version.
    """
    part = _MemoryPart()
    name = "memory-briefing-fetch"
    f_search = _run_daemon(
        name, _search_section, project, opening_query, importance_weight
    )
    f_kg = _run_daemon(name, _kg_section, project) if include_kg else None
    f_diary = _run_daemon(name, _diary_section) if include_diary else None
    search_sec, part.results_fetched, part.results_after_rerank = f_search.result()
    kg_sec = f_kg.result() if f_kg is not None else None
    diary_sec = f_diary.result() if f_diary is not None else None

    if search_sec:
        part.sections.append(search_sec)
        part.approx_tokens += len(search_sec) // 4
    if kg_sec and part.approx_tokens < token_budget:
        part.sections.append(kg_sec)
        part.approx_tokens += len(kg_sec) // 4
    if diary_sec and part.approx_tokens < token_budget:
        part.sections.append(diary_sec)
        part.approx_tokens += len(diary_sec) // 4
    return part


def _assemble_briefing(
    project: str,
    part: _MemoryPart,
    token_budget: int,
    include_project_context: bool,
) -> tuple[str, list[str], int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Memory sections + (fresh) coordination files -> the briefing text."""
    sections = list(part.sections)
    approx_tokens = part.approx_tokens

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
        return "", [], 0, part.results_fetched, part.results_after_rerank

    header = f"## Memory Briefing -- `{project}`\n"
    # No claim about history: under loop-streaming's default
    # ``ephemeral_injection_mode: persist`` the injection IS kept in the
    # conversation (and protected from compaction) for the rest of the session.
    footer = (
        "\n*Injected from memory for orientation -- not part of the user's message.*"
    )
    briefing = header + "\n\n".join(sections) + footer
    return (
        briefing,
        sections,
        approx_tokens,
        part.results_fetched,
        part.results_after_rerank,
    )


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
    - Uses the importance each search hit carries (per-hit native fact
      lookups only as a fallback for older daemons).
    - Re-ranks by ``final = semantic + weight*(importance-0.5)*0.08``.
    - Truncates to top 5 after re-ranking.
    - If ``importance_weight == 0.0``, skips the importance lookups entirely (fast path).
    """
    part = _fetch_memory_part(
        project,
        opening_query,
        token_budget,
        include_kg,
        include_diary,
        importance_weight,
    )
    return _assemble_briefing(project, part, token_budget, include_project_context)


# -- Layered briefing (T3.2, D19) --------------------------------------------


@dataclass
class _LayeredPart:
    """The daemon-derived half of a LAYERED briefing (map / standing /
    facts / evidence sections) -- the layered-mode analogue of
    :class:`_MemoryPart`, split the same way so coordination files are
    still re-read fresh at delivery (see :func:`_assemble_layered_briefing`)
    while everything daemon-derived is fetched off the critical path.
    """

    sections: list[str] = field(default_factory=list)
    approx_tokens: int = 0
    evidence_hits: list[dict[str, Any]] = field(default_factory=list)
    retrieved_hits: list[dict[str, Any]] = field(default_factory=list)
    section_counts: dict[str, int] = field(
        default_factory=lambda: {
            "map_rooms": 0,
            "standing_answered": 0,
            "standing_stale": 0,
            "facts": 0,
            "evidence": 0,
        }
    )


def _map_section(
    wing: str, retrieved_hits: list[dict[str, Any]]
) -> tuple[str | None, int]:
    """Memory map -- one line per room from the L3 index."""
    index_rows = _call_client("index", wing=wing) or []
    if isinstance(index_rows, list):
        retrieved_hits.extend(
            {"ref": r.get("scope"), "layer": "index"}
            for r in index_rows
            if isinstance(r, dict)
        )
    if not (isinstance(index_rows, list) and index_rows):
        return None, 0
    lines = ["**Memory map:**"]
    for row in index_rows:
        if not isinstance(row, dict):
            continue
        scope = row.get("scope", "") or ""
        room_name = scope.removeprefix("room:")
        abstract = (row.get("abstract", "") or "").strip()
        pending = row.get("pending_changes", 0) or 0
        marker = " (stale)" if pending > _INDEX_STALE_AFTER_DEFAULT else ""
        lines.append(f"- {room_name}: {abstract}{marker}")
    return "\n".join(lines), len(index_rows)


def _standing_section(
    wing: str, retrieved_hits: list[dict[str, Any]]
) -> tuple[str | None, int, int]:
    """Standing answers -- non-stale answers, plus one line of stale questions."""
    standing_rows = _call_client("standing", wing=wing) or []
    if isinstance(standing_rows, list):
        retrieved_hits.extend(
            {"ref": r.get("question"), "layer": "standing"}
            for r in standing_rows
            if isinstance(r, dict)
        )
    if not (isinstance(standing_rows, list) and standing_rows):
        return None, 0, 0
    answered_lines: list[str] = []
    stale_questions: list[str] = []
    for row in standing_rows:
        if not isinstance(row, dict):
            continue
        question = row.get("question", "") or ""
        if row.get("stale"):
            stale_questions.append(question)
            continue
        answer = row.get("answer", "") or ""
        answered_lines.append(f"- Q: {question} A: {answer}")
    if not (answered_lines or stale_questions):
        return None, 0, 0
    lines = ["**Standing answers:**", *answered_lines]
    if stale_questions:
        lines.append(f"- (stale, needs refresh: {'; '.join(stale_questions)})")
    return "\n".join(lines), len(answered_lines), len(stale_questions)


def _known_facts_section(
    wing: str, retrieved_hits: list[dict[str, Any]]
) -> tuple[str | None, int]:
    """Known facts -- top current L2 facts for the wing."""
    facts_rows = _call_client("facts", wing=wing, k=8) or []
    if isinstance(facts_rows, list):
        retrieved_hits.extend(
            {**r, "layer": "fact"} for r in facts_rows if isinstance(r, dict)
        )
    if not (isinstance(facts_rows, list) and facts_rows):
        return None, 0
    lines = ["**Known facts:**"]
    for f in facts_rows[:8]:
        if not isinstance(f, dict):
            continue
        text = (f.get("text") or "").strip()
        proof = f.get("proof_count", 0) or 0
        lines.append(f"- {text} [proof {proof}]")
    return "\n".join(lines), len(facts_rows)


def _evidence_section(
    wing: str,
    opening_query: str,
    project: str,
    k: int,
    retrieved_hits: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Relevant evidence -- L1 drawers as short cited snippets."""
    search_result = (
        _call_client(
            "search",
            query=opening_query or f"recent work on {project}",
            k=k,
            wing=wing,
            layers=["drawer"],
        )
        or {}
    )
    raw_hits = (
        search_result.get("results", []) if isinstance(search_result, dict) else []
    )
    retrieved_hits.extend(
        {**h, "ref": str(h.get("ref", "")), "layer": "drawer"}
        for h in raw_hits
        if isinstance(h, dict)
    )
    if not raw_hits:
        return None, []
    lines = ["**Relevant evidence:**"]
    evidence_hits: list[dict[str, Any]] = []
    for h in raw_hits:
        if not isinstance(h, dict):
            continue
        ref = str(h.get("ref", ""))
        room = h.get("room", "") or ""
        text = (h.get("content", "") or "").strip()[:300]
        lines.append(f"- [{room}] {text}")
        evidence_hits.append({"id": ref})
    return "\n".join(lines), evidence_hits


def _fetch_layered_part(
    project: str, opening_query: str, token_budget: int
) -> _LayeredPart:
    """Fetch the map / standing / facts / evidence sections CONCURRENTLY --
    the layered-mode analogue of :func:`_fetch_memory_part`. Budget gating
    is applied afterwards, in the original order, so the assembled text
    matches the sequential version."""
    wing = f"wing_{project}"
    part = _LayeredPart()
    name = "memory-briefing-fetch-layered"
    f_map = _run_daemon(name, _map_section, wing, part.retrieved_hits)
    f_standing = _run_daemon(name, _standing_section, wing, part.retrieved_hits)
    f_facts = _run_daemon(name, _known_facts_section, wing, part.retrieved_hits)
    # Evidence's k depends on remaining budget after the other three, so it
    # cannot be issued concurrently with them the same way -- it runs after
    # they resolve (matches the original sequential budget dependency).
    map_sec, map_count = f_map.result()
    standing_sec, answered_count, stale_count = f_standing.result()
    facts_sec, facts_count = f_facts.result()

    if map_sec:
        part.sections.append(map_sec)
        part.approx_tokens += len(map_sec) // 4
        part.section_counts["map_rooms"] = map_count
    if standing_sec and part.approx_tokens < token_budget:
        part.sections.append(standing_sec)
        part.approx_tokens += len(standing_sec) // 4
    part.section_counts["standing_answered"] = answered_count
    part.section_counts["standing_stale"] = stale_count
    if facts_sec and part.approx_tokens < token_budget:
        part.sections.append(facts_sec)
        part.approx_tokens += len(facts_sec) // 4
        part.section_counts["facts"] = facts_count

    if part.approx_tokens < token_budget:
        remaining = max(0, token_budget - part.approx_tokens)
        k = max(1, min(8, remaining // 40 + 1))
        evidence_sec, evidence_hits = _evidence_section(
            wing, opening_query, project, k, part.retrieved_hits
        )
        if evidence_sec:
            part.sections.append(evidence_sec)
            part.approx_tokens += len(evidence_sec) // 4
            part.evidence_hits = evidence_hits
            part.section_counts["evidence"] = len(evidence_hits)
    return part


def _assemble_layered_briefing(
    project: str,
    part: _LayeredPart,
    token_budget: int,
    include_project_context: bool,
) -> tuple[str, list[str], int, list[dict[str, Any]], dict[str, int]]:
    """Fresh coordination files (FIRST in layered mode -- legacy renders
    them last; content/budgeting logic is identical, only position moves)
    + the prefetched layered sections -> the briefing text.

    Returns ``(briefing_text, sections, token_estimate, evidence_hits,
    section_counts)``.
    """
    sections: list[str] = []
    approx_tokens = 0
    section_counts = dict(part.section_counts)
    section_counts.setdefault("coordination", 0)

    if include_project_context:
        pc_dir = _find_project_context_dir()
        if pc_dir:
            coord_section = _read_coordination_files(pc_dir, token_budget)
            if coord_section:
                sections.append(coord_section)
                approx_tokens += len(coord_section) // 4
                section_counts["coordination"] = 1

    sections.extend(part.sections)
    approx_tokens += part.approx_tokens

    if not sections:
        return "", [], 0, [], section_counts

    header = f"## Memory Briefing -- `{project}`\n"
    footer = (
        "\n*Injected from memory for orientation -- not part of the user's message.*"
    )
    briefing = header + "\n\n".join(sections) + footer
    return briefing, sections, approx_tokens, part.evidence_hits, section_counts


def _build_layered_briefing(
    project: str,
    opening_query: str,
    token_budget: int,
    include_project_context: bool = True,
) -> tuple[str, list[str], int, list[dict[str, Any]], dict[str, int]]:
    """Synchronous entry point mirroring :func:`_build_briefing` -- fetch
    then assemble, both stages inline (used by the direct ``__call__`` path;
    the background-prefetch path calls :func:`_fetch_layered_part` and
    :func:`_assemble_layered_briefing` separately, see :func:`_prefetch_job`)."""
    part = _fetch_layered_part(project, opening_query, token_budget)
    return _assemble_layered_briefing(
        project, part, token_budget, include_project_context
    )


# -- Background prefetch (off the session's critical path) --------------------

#: Outcome kinds a prefetch job can produce.
_OK = "ok"
_DAEMON_UNAVAILABLE = "daemon_unavailable"


@dataclass
class _Prefetch:
    kind: str
    project: str
    part: _MemoryPart | None = None
    #: P3 (T3.2, D19): the effective mode this prefetch actually ran in
    #: ("layered" or "legacy") -- resolved once, on the prefetch thread, via
    #: a capability probe (``hasattr(client, "index")``), so delivery never
    #: re-probes or mixes assembly functions with the wrong part type.
    mode: str = "legacy"
    layered_part: _LayeredPart | None = None
    #: False when any lookup failed (timeout, HTTP error, ...): the partial
    #: briefing is still delivered, but never reused from the cache.
    complete: bool = True
    #: T4.1: wall-clock time the daemon-derived fetch itself took, measured
    #: on the prefetch thread (delivery only reports it -- waiting for the
    #: prefetch is not part of "how long did retrieval take").
    fetch_latency_ms: float = 0.0


_PREFETCH_CACHE: dict[tuple[Any, ...], tuple[float, concurrent.futures.Future]] = {}
_PREFETCH_LOCK = threading.Lock()


def _run_daemon(name: str, fn: Any, *args: Any) -> concurrent.futures.Future:
    """Run ``fn(*args)`` on a DAEMON thread, returning a Future.

    ``ThreadPoolExecutor`` workers are joined at interpreter exit, so a
    session that ended before a (cold, slow) prefetch finished used to hold
    the process open until it did. Briefing work is disposable: at exit it
    is simply abandoned.
    """
    fut: concurrent.futures.Future = concurrent.futures.Future()

    def _runner() -> None:
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - surfaced via the Future
            fut.set_exception(exc)

    threading.Thread(target=_runner, name=name, daemon=True).start()
    return fut


def _prefetch_job(
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    importance_weight: float,
    briefing_mode: str,
    ensure: Any,
) -> _Prefetch:
    """Everything slow, on a worker thread: project detection (git
    subprocess), daemon discovery/spawn, and the memory lookups.

    ``ensure`` is the ``ensure_daemon`` resolved on the CALLER's thread when
    the prefetch was started (so a test's patch is honoured even if the job
    runs after the patch is undone).

    P3 (T3.2, D19): ``briefing_mode`` is resolved to an EFFECTIVE mode here,
    once, via a capability probe (an older pinned daemon missing the L3 ops
    -- no ``index`` method on the client -- degrades to legacy automatically,
    same pattern as hooks-memory-reflect's ``hasattr`` checks).
    """
    project = _detect_project_name()
    client = ensure()
    if client is None:
        return _Prefetch(kind=_DAEMON_UNAVAILABLE, project=project)
    effective_mode = briefing_mode
    if effective_mode == "layered" and not hasattr(client, "index"):
        effective_mode = "legacy"
    failures_before = _CALL_FAILURES
    t0 = time.monotonic()
    if effective_mode == "layered":
        layered_part = _fetch_layered_part(project, "", token_budget)
        return _Prefetch(
            kind=_OK,
            project=project,
            mode="layered",
            layered_part=layered_part,
            complete=_CALL_FAILURES == failures_before,
            fetch_latency_ms=(time.monotonic() - t0) * 1000,
        )
    part = _fetch_memory_part(
        project, "", token_budget, include_kg, include_diary, importance_weight
    )
    return _Prefetch(
        kind=_OK,
        project=project,
        mode="legacy",
        part=part,
        complete=_CALL_FAILURES == failures_before,
        fetch_latency_ms=(time.monotonic() - t0) * 1000,
    )


def _start_prefetch(
    token_budget: int,
    include_kg: bool,
    include_diary: bool,
    importance_weight: float,
    briefing_mode: str,
    cache_ttl_s: float,
) -> concurrent.futures.Future:
    """Start (or reuse) the per-process prefetch for this cwd + config.

    Cached per ``(memory home, cwd, config)`` for ``cache_ttl_s``: sibling
    sessions in the same process (sub-agents, server/TUI hosts running many
    sessions) share one fetch instead of each paying the daemon round-trips.
    A failed, partially failed (any lookup errored) or daemon-unavailable
    result is never reused.
    """
    try:
        from amplifier_module_tool_memory.daemon import default_memory_home

        home = str(default_memory_home())
    except Exception:
        home = ""
    key = (
        home,
        os.getcwd(),
        token_budget,
        include_kg,
        include_diary,
        importance_weight,
        briefing_mode,
    )
    now = time.monotonic()
    with _PREFETCH_LOCK:
        hit = _PREFETCH_CACHE.get(key)
        if hit is not None:
            started, fut = hit
            reusable = (now - started) < cache_ttl_s and not (
                fut.done()
                and (
                    fut.exception() is not None
                    or fut.result().kind != _OK
                    or not fut.result().complete
                )
            )
            if reusable:
                return fut
    fut = _run_daemon(
        "memory-briefing",
        _prefetch_job,
        token_budget,
        include_kg,
        include_diary,
        importance_weight,
        briefing_mode,
        ensure_daemon,
    )
    with _PREFETCH_LOCK:
        _PREFETCH_CACHE[key] = (now, fut)
    return fut


def _reset_briefing_cache() -> None:
    """Drop the per-process prefetch cache (tests / benches)."""
    with _PREFETCH_LOCK:
        _PREFETCH_CACHE.clear()


def _wait_for_background(timeout: float = 30.0) -> None:
    """Block until every in-flight prefetch finished (tests / benches)."""
    with _PREFETCH_LOCK:
        futs = [f for _, f in _PREFETCH_CACHE.values()]
    concurrent.futures.wait(futs, timeout=timeout)


# -- Hook class --------------------------------------------------------------


class MemoryBriefingHook:
    name = "hooks-memory-briefing"
    #: Events ``mount()`` wires: ``session:start`` arms delivery;
    #: ``prompt:submit`` / ``provider:request`` deliver the prefetched
    #: briefing (once per session).
    events = ["session:start", "prompt:submit", "provider:request"]

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
        # Longest the FIRST prompt:submit may wait for a still-running
        # prefetch before letting the turn proceed (later delivery points
        # never wait).
        self.deliver_wait_s: float = max(
            0.0, float(self.config.get("deliver_wait_s", 2.0))
        )
        # Per-process reuse window for prefetched memory sections.
        self.cache_ttl_s: float = max(0.0, float(self.config.get("cache_ttl_s", 300.0)))
        # Sub-agent sessions (session:start with a parent_id) get no
        # briefing by default: they run a delegated, already-scoped task and
        # would otherwise pay the briefing's tokens on every spawn.
        self.brief_subsessions: bool = bool(self.config.get("brief_subsessions", False))
        # perf/incremental-fold (part B): automated/non-interactive runs
        # opt out entirely -- see amplifier_module_tool_memory.automation_gate.
        self.excluded_working_dirs: list[str] = list(
            self.config.get("excluded_working_dirs", []) or []
        )
        # P3 (T3.2, D19): "layered" builds the Memory map / Standing answers /
        # Known facts / Relevant evidence briefing; "legacy" is the pre-P3
        # behavior, byte-identical. Configured default is "layered", but the
        # daemon client is probed via hasattr("index") on the prefetch thread
        # (T3.1 capability check, not a hard dependency) -- an older pinned
        # daemon without the L3 ops degrades to legacy automatically.
        self.briefing_mode: str = self.config.get("briefing_mode", "layered")

        self._bridge_emit: AsyncBridge = bridge_emit or NOOP_ASYNC_BRIDGE

        # Per-session delivery state (one hook instance per mounted session).
        self._future: concurrent.futures.Future | None = None
        self._armed = False
        self._delivered = False
        self._deferred_emitted = False
        self._sid: Any = None

    # -- background mode (what mount() wires) --------------------------------

    def prefetch(self) -> None:
        """Start the background fetch now (idempotent). Never raises."""
        if self._future is not None:
            return
        try:
            self._future = _start_prefetch(
                self.token_budget,
                self.include_kg,
                self.include_diary,
                self.briefing_importance_weight,
                self.briefing_mode,
                self.cache_ttl_s,
            )
        except Exception:
            self._future = None

    async def on_session_start(self, event: str, data: dict[str, Any]) -> HookResult:
        """Arm delivery for this session; never blocks on memory I/O."""
        self._sid = data.get("session_id")
        if data.get("parent_id") and not self.brief_subsessions:
            return HookResult(action="continue")
        # perf/incremental-fold (part B): automated/non-interactive runs
        # never get armed -- no prefetch, no daemon contact, nothing.
        if automation_opt_out(excluded_working_dirs=self.excluded_working_dirs):
            return HookResult(action="continue")
        self._armed = True
        self.prefetch()
        return HookResult(action="continue")

    async def on_deliver(self, event: str, data: dict[str, Any]) -> HookResult:
        """Inject the prefetched briefing at the first delivery point it is
        ready for. Only the first ``prompt:submit`` may wait, and only up to
        ``deliver_wait_s``; every other call is a non-blocking readiness
        check."""
        if not self._armed or self._delivered:
            return HookResult(action="continue")
        if event == "provider:request" and data.get("iteration") == 0:
            # loop-streaming's internal side calls (e.g. the goal stall
            # judge) emit provider:request with iteration 0 and drop any
            # injection -- never spend the one-shot delivery there.
            return HookResult(action="continue")
        self.prefetch()
        fut = self._future
        if fut is None:
            self._delivered = True
            return HookResult(action="continue")

        wait = (
            self.deliver_wait_s
            if (event == "prompt:submit" and not self._deferred_emitted)
            else 0.0
        )
        if not fut.done() and wait > 0:
            # Poll rather than wrap_future(): no cross-thread callback into
            # the event loop that could outlive it.
            deadline = time.monotonic() + wait
            while not fut.done() and time.monotonic() < deadline:
                await asyncio.sleep(_POLL_S)
        if not fut.done():
            if not self._deferred_emitted:
                self._deferred_emitted = True
                if self.emit_events:
                    emit_event(
                        "memory-briefing",
                        "briefing_deferred",
                        ok=True,
                        data={"event": event, "waited_s": wait},
                        session_id=self._sid,
                    )
            return HookResult(action="continue")

        self._delivered = True
        try:
            pre: _Prefetch = fut.result()
        except Exception as exc:
            if self.emit_events:
                emit_event(
                    "memory-briefing",
                    "briefing_skipped",
                    ok=False,
                    data={"reason": "prefetch_failed", "error": str(exc)[:200]},
                    session_id=self._sid,
                )
            return HookResult(action="continue")

        if pre.kind != _OK:
            return await self._daemon_unavailable_result(self._sid)
        if pre.mode == "layered" and pre.layered_part is not None:
            (
                briefing,
                sections,
                token_estimate,
                evidence_hits,
                _section_counts,
            ) = _assemble_layered_briefing(
                pre.project,
                pre.layered_part,
                self.token_budget,
                self.include_project_context,
            )
            return await self._briefing_result(
                pre.project,
                (briefing, sections, token_estimate, [], evidence_hits),
                self._sid,
                mode="layered",
                retrieved_hits=pre.layered_part.retrieved_hits,
                latency_ms=pre.fetch_latency_ms,
            )
        if pre.part is None:
            return await self._daemon_unavailable_result(self._sid)
        return await self._briefing_result(
            pre.project,
            _assemble_briefing(
                pre.project, pre.part, self.token_budget, self.include_project_context
            ),
            self._sid,
            mode="legacy",
            latency_ms=pre.fetch_latency_ms,
        )

    # -- synchronous entry point ----------------------------------------------

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        """Build and return the briefing synchronously (blocking).

        Kept as the direct, self-contained entry point (tests, embedding
        hosts whose kernels honor a returned injection wherever they call
        it). ``mount()`` no longer registers this on ``session:start`` --
        see the module docstring.
        """
        sid = data.get("session_id")

        # Check if the memory daemon is reachable/spawnable -- skip the
        # memory-derived sections silently if not (native cutover: replaces
        # the old vendor CLI version probe).
        client = ensure_daemon()
        if client is None:
            return await self._daemon_unavailable_result(sid)

        project = _detect_project_name()
        opening_query = data.get("opening_prompt", "") or data.get("prompt", "")

        # P3 (T3.2, D19): capability probe, not a hard dependency -- an older
        # pinned daemon missing the L3 ops (no `index` method on the client)
        # degrades to legacy automatically.
        effective_mode = self.briefing_mode
        if effective_mode == "layered" and not hasattr(client, "index"):
            effective_mode = "legacy"

        t0 = time.monotonic()
        if effective_mode == "layered":
            part = _fetch_layered_part(project, opening_query, self.token_budget)
            briefing, sections, token_estimate, evidence_hits, _section_counts = (
                _assemble_layered_briefing(
                    project, part, self.token_budget, self.include_project_context
                )
            )
            latency_ms = (time.monotonic() - t0) * 1000
            return await self._briefing_result(
                project,
                (briefing, sections, token_estimate, [], evidence_hits),
                sid,
                mode="layered",
                retrieved_hits=part.retrieved_hits,
                latency_ms=latency_ms,
            )

        built = _build_briefing(
            project=project,
            opening_query=opening_query,
            token_budget=self.token_budget,
            include_kg=self.include_kg,
            include_diary=self.include_diary,
            include_project_context=self.include_project_context,
            importance_weight=self.briefing_importance_weight,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        return await self._briefing_result(
            project, built, sid, mode="legacy", latency_ms=latency_ms
        )

    # -- shared result/emission paths ----------------------------------------

    async def _daemon_unavailable_result(self, sid: Any) -> HookResult:
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
                    footer = (
                        "\n*Memory daemon not available -- semantic search skipped.*"
                    )
                    return HookResult(
                        action="inject_context",
                        context_injection=header + section + footer,
                        context_injection_role="user",
                        ephemeral=self.ephemeral,
                        suppress_output=True,
                    )
        return HookResult(action="continue")

    async def _briefing_result(
        self,
        project: str,
        built: tuple[str, list[str], int, list[dict[str, Any]], list[dict[str, Any]]],
        sid: Any,
        *,
        mode: str = "legacy",
        retrieved_hits: list[dict[str, Any]] | None = None,
        latency_ms: float = 0.0,
    ) -> HookResult:
        briefing, sections, token_estimate, results_fetched, results_after_rerank = (
            built
        )

        # Derive drawer_ids from results_after_rerank (list of dicts)
        drawer_ids = [
            r["id"]
            for r in (results_after_rerank or [])
            if isinstance(r, dict) and "id" in r
        ]

        # T4.1: one memory:retrieved per briefing assembly, summarizing every
        # search/fact/index read this pass made (mechanism-first: the
        # conductor -- not this hook -- decides what "helped" means).
        # ``retrieved_hits`` defaults to the legacy mode's own rerank results
        # (shape ``{"id": ref, ...}``) when the caller (legacy path) has no
        # dedicated hits collection of its own -- layered mode always passes
        # its own (index/standing/fact/drawer) hits explicitly.
        hits = (
            retrieved_hits
            if retrieved_hits is not None
            else [
                {**r, "ref": r.get("id"), "layer": "drawer"}
                for r in (results_after_rerank or [])
                if isinstance(r, dict)
            ]
        )
        if self.emit_events:
            retrieved_data = build_retrieved_data(
                source="briefing",
                op=mode,
                query="",
                wing=f"wing_{project}",
                hits=hits,
                latency_ms=latency_ms,
            )
            try:
                emit_event(
                    "memory-briefing",
                    "retrieved",
                    ok=True,
                    data=retrieved_data,
                    session_id=sid,
                )
            except Exception:
                pass
            try:
                await self._bridge_emit(
                    "memory:retrieved", {"ok": True, **retrieved_data}
                )
            except Exception:
                pass

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
                        "mode": mode,
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

                # T4.1: memory:injected -- the briefing actually entered the
                # model context (unlike memory:retrieved, which fires
                # regardless of outcome). Refs are content addresses,
                # stable across sessions.
                injected_refs = [
                    str(h.get("ref") or h.get("id") or "")
                    for h in hits
                    if h.get("ref") or h.get("id")
                ]
                injected_layers = {
                    str(h.get("ref") or h.get("id")): h.get("layer", "drawer")
                    for h in hits
                    if h.get("ref") or h.get("id")
                }
                injected_data = {
                    "source": "briefing",
                    "refs": injected_refs,
                    "layers": injected_layers,
                    "chars": len(briefing),
                }
                try:
                    emit_event(
                        "memory-briefing",
                        "injected",
                        ok=True,
                        data=injected_data,
                        session_id=sid,
                    )
                except Exception:
                    pass
                try:
                    await self._bridge_emit(
                        "memory:injected", {"ok": True, **injected_data}
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


#: Readiness poll interval while the first prompt waits for the prefetch.
_POLL_S = 0.02

#: Runs before hooks-memory-interject's prompt:submit handler (priority 20)
#: so ``memory:briefing_assembled`` populates interject's already-briefed ids
#: before it searches.
_DELIVER_PRIORITY = 10


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the memory-briefing hook into the Amplifier coordinator."""
    register_events(
        coordinator,
        "memory-briefing",
        [
            "memory:briefing_assembled",
            "memory:briefing_skipped",
            "memory:retrieved",
            "memory:injected",
        ],
    )

    bridge_emit = make_async_bridge(coordinator)

    hook = MemoryBriefingHook(config, bridge_emit=bridge_emit)
    coordinator.hooks.register("session:start", hook.on_session_start, name=hook.name)
    for event in ("prompt:submit", "provider:request"):
        coordinator.hooks.register(
            event,
            hook.on_deliver,
            priority=_DELIVER_PRIORITY,
            name=f"{hook.name}-deliver",
        )
    # Start the daemon lookups now, in the background: in an interactive
    # session they finish while the user is still typing. perf/incremental-
    # fold (part B): skipped entirely for an opted-out automated run -- this
    # early prefetch runs at mount() time, BEFORE session:start, so it is a
    # second, independent call site that must honor the same opt-out
    # on_session_start does (mount() has no parent_id to check yet, hence
    # no subsession skip here -- only the automation opt-out applies).
    if not automation_opt_out(
        excluded_working_dirs=list(config.get("excluded_working_dirs", []) or [])
        if config
        else []
    ):
        hook.prefetch()
    return {
        "name": "hooks-memory-briefing",
        "version": "2.2.0",
        "provides": ["memory-briefing"],
    }
