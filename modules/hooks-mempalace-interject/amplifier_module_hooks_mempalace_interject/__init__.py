"""
amplifier-module-hooks-mempalace-interject
==========================================
Amplifier hook that surfaces relevant memories mid-session at the right
moment — non-disruptive, contemplative, and timely.

Design (from Amplifier expert consultation):
  - Registers on THREE events with separate handlers (OR-firing pattern):
      1. prompt:submit   — check if the user's new prompt triggers a memory
      2. tool:pre        — check if this tool+input was seen before
      3. orchestrator:complete — check if the response contradicts a memory
  - Relevance gate: cosine similarity threshold (primary) + optional LLM
    judge (secondary, only when cosine score is in the "uncertain" band)
  - inject_context with ephemeral=True (safe default; avoids context bloat)
  - Priority: 20 (runs early, after critical instrumentation at 50+)
  - Per-turn guard flag prevents infinite loops on orchestrator:complete

Read lane (fixed 2026-07-07 -- was reading the wrong store entirely)
---------------------------------------------------------------------
Previously this hook opened a ChromaDB ``PersistentClient`` directly at a
hardcoded ``~/.mempalace/chroma`` path / ``mempalace_default`` collection.
Neither value is real: mempalace (verified against the installed 3.5.0
package, ``mempalace/config.py``) actually writes to ``~/.mempalace/palace``
with collection ``mempalace_drawers``. The write lane (mempalace CLI /
hooks-mempalace-capture) and this hook's read lane never shared a store --
interject could never see anything the palace actually held, even after the
MCP transport bug in tool-mempalace was fixed separately.

The fix: this hook no longer touches ChromaDB (or any path/collection name)
directly. It reads exclusively through mempalace's own supported surface --
the ``mempalace_search`` tool over the real ``mempalace-mcp`` JSON-RPC-over-
stdio server (same ``_call_mcp_tool`` pattern as hooks-mempalace-capture and
hooks-mempalace-briefing). mempalace resolves its OWN configured palace path
and collection name server-side, so there is no path/collection value left
for this hook to get wrong -- read and write lanes are structurally the same
store by construction. It also means this hook never embeds the query
itself: mempalace embeds it internally with whatever local model the palace
was actually written with (see Privacy below), so retrieval is always in
the same vector space the drawers were stored in.

Privacy
-------
By default this hook makes ZERO external network calls. Retrieval goes
through mempalace's local ``mempalace_search`` (mempalace's own configured
embedding model -- ``all-MiniLM-L6-v2`` via ONNX Runtime by default, fully
offline; see ``mempalace/embedding.py``). The ONLY external call this hook
can ever make is the OPTIONAL LLM judge (OpenAI ``gpt-4.1-nano``), used to
refine borderline-relevance ("uncertain band") matches. It is gated behind
``llm_judge_enabled`` (default ``False``) -- nothing leaves the machine
unless a user explicitly opts in. When enabled, it sends the current query
text and candidate memory snippets to OpenAI using ``OPENAI_API_KEY``.

Configuration keys (all optional):
  cosine_threshold:  float = 0.72   # minimum similarity to inject
  uncertain_band:    float = 0.10   # band above threshold that triggers LLM judge
  max_inject_chars:  int   = 800    # max chars per injection
  cooldown_turns:    int   = 3      # min turns between injections for same memory
  tool_pre_enabled:  bool  = True   # enable tool:pre handler
  prompt_enabled:    bool  = True   # enable prompt:submit handler
  orc_enabled:       bool  = True   # enable orchestrator:complete handler
  llm_judge_enabled: bool  = False  # OPT-IN: sends query + memory text to OpenAI
                                    # (gpt-4.1-nano) for uncertain-band scoring.
                                    # Off by default -- see Privacy above.
  emit_events:       bool  = True   # emit JSONL events
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from typing import Any

try:
    from amplifier_core import HookResult, HookRegistry  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests)
    class HookResult:  # type: ignore
        def __init__(self, *, action="continue", **kwargs):
            self.action = action
            for k, v in kwargs.items():
                setattr(self, k, v)

    class HookRegistry:  # type: ignore
        SESSION_START = "session:start"
        SESSION_END = "session:end"
        TOOL_PRE = "tool:pre"
        TOOL_POST = "tool:post"
        PROMPT_SUBMIT = "prompt:submit"
        ORCHESTRATOR_COMPLETE = "orchestrator:complete"
        CONTEXT_PRE_COMPACT = "context:pre_compact"


try:
    from amplifier_module_tool_mempalace.event_emitter import emit_event
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


try:
    from amplifier_module_tool_mempalace.coordinator_bridge import (
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


try:
    # Canonical MCP JSON-RPC-over-stdio helper. Declared as a real dependency
    # in pyproject.toml (amplifier-module-tool-mempalace>=1.0.0), so this
    # import succeeds in any properly installed environment; the fallback
    # below only triggers in degraded/partial environments, matching the
    # existing defensive-import convention used throughout this file and
    # mirrored in hooks-mempalace-capture / hooks-mempalace-briefing.
    from amplifier_module_tool_mempalace.scripts.memory_store import (
        _call_mcp_tool as _call_mcp_tool_impl,
    )
except ImportError:
    # Minimal private copy of the canonical helper in
    # amplifier_module_tool_mempalace/scripts/memory_store.py::_call_mcp_tool.
    # Keep in sync with that implementation if mempalace's MCP wire format
    # changes; this is a fallback for degraded environments only, not a
    # second source of truth.
    def _call_mcp_tool_impl(  # type: ignore[misc]
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
        call_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        stdin_payload = json.dumps(init_req) + "\n" + json.dumps(call_req) + "\n"
        try:
            proc = subprocess.run(
                ["mempalace-mcp"],
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        call_response: dict[str, Any] | None = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == 2:
                call_response = msg
                break
        if call_response is None:
            return {
                "error": (
                    f"mempalace-mcp produced no tools/call response for "
                    f"{tool_name!r} (rc={proc.returncode}). "
                    f"stderr: {proc.stderr.strip()[:500]}"
                )
            }
        if "error" in call_response:
            rpc_error = call_response["error"]
            message = (
                rpc_error.get("message") if isinstance(rpc_error, dict) else rpc_error
            )
            return {"error": message or str(rpc_error)}
        content_ = (call_response.get("result") or {}).get("content") or []
        text_out = content_[0].get("text") if content_ else None
        if text_out is None:
            return {"error": "tools/call result missing content[0].text"}
        try:
            return json.loads(text_out)
        except json.JSONDecodeError:
            return {"raw": text_out}


# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_COSINE_THRESHOLD = 0.72
DEFAULT_UNCERTAIN_BAND = 0.10
DEFAULT_MAX_INJECT_CHARS = 800
DEFAULT_COOLDOWN_TURNS = 3

# Documented for operator visibility ONLY -- NOT used to construct any path
# or open any store at runtime. Retrieval routes exclusively through
# mempalace's own mempalace_search MCP tool (see _mcp_search below), which
# resolves its OWN configured palace path / collection name server-side.
# Verified against the installed mempalace 3.5.0 package source
# (mempalace/config.py: DEFAULT_PALACE_PATH, DEFAULT_COLLECTION_NAME).
# Pinned by tests/test_store_alignment.py against the real package when
# importable, so drift in a future mempalace release is caught here.
DOCUMENTED_MEMPALACE_PALACE_PATH = "~/.mempalace/palace"
DOCUMENTED_MEMPALACE_COLLECTION_NAME = "mempalace_drawers"

# ── LLM judge ──────────────────────────────────────────────────────────────────


async def _llm_judge(query: str, memory_text: str) -> float:
    """Ask a fast LLM to score the relevance of a memory to the current query.

    Returns a float in [0, 1]. Uses gpt-4.1-nano for speed and cost.
    """
    try:
        from openai import AsyncOpenAI  # type: ignore

        client = AsyncOpenAI()
        prompt = (
            "Rate how relevant this memory is to the current query. "
            "Reply with ONLY a number from 0.0 (irrelevant) to 1.0 (highly relevant).\n\n"
            f"QUERY: {query[:500]}\n\n"
            f"MEMORY: {memory_text[:500]}"
        )
        resp = await client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0,
        )
        score_str = (resp.choices[0].message.content or "").strip()
        return min(1.0, max(0.0, float(score_str)))
    except Exception:
        return 0.0


# ── MCP-based retrieval ─────────────────────────────────────────────


def _derive_memory_id(hit: dict[str, Any], text: str) -> str:
    """Stable surrogate id for a search hit lacking a real drawer id.

    mempalace 3.5.0's ``mempalace_search`` does not return a drawer id in
    its hit shape (verified against the installed package,
    ``mempalace/searcher.py``'s hit-entry construction) -- only
    ``source_path``/``source_file`` and content. Hash ``source_path`` +
    a text prefix so the same drawer content produces the same id across
    repeat retrievals; cooldown / already-briefed dedup only need
    stability, not a real palace identifier.
    """
    basis = f"{hit.get('source_path', '')}:{text[:200]}"
    return hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _mcp_search(query: str, n_results: int = 5) -> list[dict[str, Any]]:
    """Retrieve candidate memories via mempalace's own supported MCP surface.

    Calls the real ``mempalace_search`` tool over the ``mempalace-mcp``
    JSON-RPC-over-stdio server (see ``_call_mcp_tool_impl`` above -- the same
    helper hooks-mempalace-capture and hooks-mempalace-briefing use). This is
    the ONLY way this hook reads the palace: mempalace resolves its OWN
    configured palace path and collection name server-side, so there is no
    path or collection value left for this hook to get wrong. It also means
    this hook never embeds the query itself -- mempalace embeds it
    internally with whatever local model the palace was actually written
    with, so retrieval always happens in the same vector space the drawers
    were stored in.

    (Previously this function opened a ChromaDB ``PersistentClient``
    directly at a hardcoded ``~/.mempalace/chroma`` / ``mempalace_default``
    -- neither value is real; mempalace actually writes to
    ``~/.mempalace/palace`` / ``mempalace_drawers``, verified against the
    installed mempalace 3.5.0 package. See ``DOCUMENTED_MEMPALACE_*``
    constants above and ``tests/test_store_alignment.py``.)

    Returns a list of dicts with keys: id, text, score, metadata. ``score``
    is mempalace's own cosine similarity (``max(0, 1 - distance)``,
    ``mempalace/searcher.py:_distance_to_similarity``) -- never crashes the
    session; any transport or tool-level failure yields an empty list.
    """
    if not query:
        return []
    result = _call_mcp_tool_impl(
        "mempalace_search",
        # mempalace_search's query field is capped at 250 chars (its own
        # input_schema maxLength) -- truncate before sending, not after.
        {"query": query[:250], "limit": n_results},
    )
    if not isinstance(result, dict) or result.get("error"):
        return []
    hits = result.get("results")
    if not isinstance(hits, list):
        return []
    memories: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text", "") or ""
        mem_id = hit.get("id") or _derive_memory_id(hit, text)
        memories.append(
            {
                "id": mem_id,
                "text": text,
                "score": float(hit.get("similarity", 0.0) or 0.0),
                "metadata": {
                    "wing": hit.get("wing"),
                    "room": hit.get("room"),
                    "source_file": hit.get("source_file"),
                },
            }
        )
    return memories


# ── Injection formatter ────────────────────────────────────────────# ── Injection formatter ───────────────────────────────────────────────────────


def _format_injection(
    memories: list[dict[str, Any]], event: str, max_chars: int
) -> str:
    """Format retrieved memories into a concise injection block."""
    if not memories:
        return ""

    if event == HookRegistry.PROMPT_SUBMIT:
        header = "📚 Relevant memory from a previous session:"
    elif event == HookRegistry.TOOL_PRE:
        header = "🔁 You've done something similar before — here's what happened:"
    else:  # orchestrator:complete
        header = "⚠️ Memory check — this may be relevant to your current reasoning:"

    parts = [header]
    total = len(header)
    for mem in memories:
        snippet = mem["text"].strip()
        if total + len(snippet) > max_chars:
            snippet = snippet[: max_chars - total - 10] + "…"
        parts.append(f"\n---\n{snippet}")
        total += len(snippet)
        if total >= max_chars:
            break

    return "\n".join(parts)


# ── Main hook class ────────────────────────────────────────────────────────────


class MempalaceInterjectHook:
    """OR-firing hook that injects relevant memories at the right moment.

    Registers on prompt:submit, tool:pre, and orchestrator:complete.
    Uses cosine similarity as the primary relevance gate, with an optional
    LLM judge for scores in the uncertain band.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        bridge_emit: AsyncBridge | None = None,
    ) -> None:
        config = config or {}
        self.cosine_threshold: float = float(
            config.get("cosine_threshold", DEFAULT_COSINE_THRESHOLD)
        )
        self.uncertain_band: float = float(
            config.get("uncertain_band", DEFAULT_UNCERTAIN_BAND)
        )
        self.max_inject_chars: int = int(
            config.get("max_inject_chars", DEFAULT_MAX_INJECT_CHARS)
        )
        self.cooldown_turns: int = int(
            config.get("cooldown_turns", DEFAULT_COOLDOWN_TURNS)
        )
        self.prompt_enabled: bool = bool(config.get("prompt_enabled", True))
        self.tool_pre_enabled: bool = bool(config.get("tool_pre_enabled", True))
        self.orc_enabled: bool = bool(config.get("orc_enabled", True))
        # OPT-IN, default False: gates the ONLY external network call this
        # hook can make (OpenAI gpt-4.1-nano for uncertain-band scoring).
        # See module docstring "Privacy" section.
        self.llm_judge_enabled: bool = bool(config.get("llm_judge_enabled", False))
        self.emit_events: bool = bool(config.get("emit_events", True))

        # Per-turn guard flag: prevents re-injection in the same orchestrator run
        self._injected_this_turn: bool = False
        # Cooldown tracker: memory_id → last injection turn number
        self._last_injected: dict[str, int] = {}
        # Turn counter (incremented on orchestrator:complete)
        self._turn: int = 0
        # Briefing memory IDs (populated via cross-hook briefing_assembled listener)
        self._briefed_ids: set[str] = set()
        # Coordinator bridge emit function
        self._bridge_emit: AsyncBridge = bridge_emit or NOOP_ASYNC_BRIDGE

    def _is_on_cooldown(self, memory_id: str) -> bool:
        """Check if a memory was recently injected (within cooldown_turns)."""
        last = self._last_injected.get(memory_id, -999)
        return (self._turn - last) < self.cooldown_turns

    def _mark_injected(self, memory_ids: list[str]) -> None:
        """Record that these memories were just injected."""
        for mid in memory_ids:
            self._last_injected[mid] = self._turn

    async def _retrieve_and_gate(
        self, query: str, event: str
    ) -> tuple[list[dict[str, Any]], bool, str, bool]:
        """Retrieve memories and decide whether to inject.

        Returns (memories_to_inject, should_inject, skip_reason, judge_used).
        skip_reason is only meaningful when should_inject is False.
        """
        # Retrieve top candidates via mempalace's own MCP surface. This is a
        # blocking subprocess call (mempalace-mcp), so it runs off the event
        # loop the same way the old OpenAI-embedding call did.
        candidates = await asyncio.get_event_loop().run_in_executor(
            None, _mcp_search, query, 5
        )
        if not candidates:
            return [], False, "retrieval_failed", False

        # Filter out already-briefed and on-cooldown memories
        candidates = [
            m
            for m in candidates
            if m["id"] not in self._briefed_ids and not self._is_on_cooldown(m["id"])
        ]
        if not candidates:
            return [], False, "cooldown", False

        # Primary gate: cosine similarity threshold
        above_threshold = [m for m in candidates if m["score"] >= self.cosine_threshold]
        uncertain = [
            m
            for m in candidates
            if self.cosine_threshold - self.uncertain_band
            <= m["score"]
            < self.cosine_threshold
        ]

        judge_used = False
        # Secondary gate: LLM judge for uncertain-band memories -- OPT-IN
        # only (self.llm_judge_enabled, default False). This is the ONLY
        # external network call this hook can make; see module docstring
        # "Privacy" section. When disabled, uncertain-band candidates are
        # simply not promoted -- the primary cosine gate is the sole arbiter.
        if uncertain and self.llm_judge_enabled:
            judge_tasks = [_llm_judge(query, m["text"]) for m in uncertain]
            judge_scores = await asyncio.gather(*judge_tasks)
            judge_used = True
            for mem, js in zip(uncertain, judge_scores):
                if js >= 0.7:  # LLM judge threshold
                    above_threshold.append(mem)

        if not above_threshold:
            return [], False, "below_threshold", judge_used

        # Take top 2 by score
        top = sorted(above_threshold, key=lambda m: m["score"], reverse=True)[:2]
        return top, True, "", judge_used

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_prompt_submit(self, event: str, data: dict[str, Any]) -> HookResult:
        """Fire on prompt:submit — inject before the LLM sees the user's prompt."""
        sid = data.get("session_id")

        if not self.prompt_enabled:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "prompt_submit", "reason": "disabled"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {"ok": False, "trigger": "prompt_submit", "reason": "disabled"},
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        prompt_text = data.get("prompt", "") or data.get("content", "")
        if not prompt_text or len(prompt_text) < 20:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "prompt_submit", "reason": "too_short"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "prompt_submit",
                            "reason": "too_short",
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        # Reset per-turn guard (new user prompt = new turn)
        self._injected_this_turn = False

        (
            memories,
            should_inject,
            skip_reason,
            judge_used,
        ) = await self._retrieve_and_gate(prompt_text, event)
        if not should_inject:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "prompt_submit", "reason": skip_reason},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "prompt_submit",
                            "reason": skip_reason,
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        injection = _format_injection(memories, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in memories])
        self._injected_this_turn = True

        if self.emit_events:
            top_score = max(m["score"] for m in memories) if memories else 0.0
            emit_event(
                "mempalace-interject",
                "memory_surfaced",
                ok=True,
                preview=injection[:100] if injection else None,
                data={
                    "trigger": "prompt_submit",
                    "memory_ids": [m["id"] for m in memories],
                    "top_score": top_score,
                    "judge_used": judge_used,
                },
                session_id=sid,
            )
            try:
                await self._bridge_emit(
                    "memory-mempalace:memory_surfaced",
                    {
                        "ok": True,
                        "preview": injection[:100] if injection else None,
                        "trigger": "prompt_submit",
                        "memory_ids": [m["id"] for m in memories],
                        "top_score": top_score,
                        "judge_used": judge_used,
                    },
                )
            except Exception:
                pass

        return HookResult(
            action="inject_context",
            context_injection=injection,
            context_injection_role="system",
            ephemeral=True,  # safe default: don't persist in conversation history
        )

    async def on_tool_pre(self, event: str, data: dict[str, Any]) -> HookResult:
        """Fire on tool:pre — surface prior results for the same tool+input."""
        sid = data.get("session_id")

        if not self.tool_pre_enabled:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "tool_pre", "reason": "disabled"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {"ok": False, "trigger": "tool_pre", "reason": "disabled"},
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        # Build a query from tool name + key input fields
        query_parts = [f"tool:{tool_name}"]
        for key in ("path", "command", "query", "url", "file_path"):
            if key in tool_input:
                query_parts.append(str(tool_input[key])[:200])
        query = " ".join(query_parts)

        if len(query) < 15:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "tool_pre", "reason": "too_short"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {"ok": False, "trigger": "tool_pre", "reason": "too_short"},
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        (
            memories,
            should_inject,
            skip_reason,
            judge_used,
        ) = await self._retrieve_and_gate(query, event)
        if not should_inject:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "tool_pre", "reason": skip_reason},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {"ok": False, "trigger": "tool_pre", "reason": skip_reason},
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        injection = _format_injection(memories, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in memories])

        if self.emit_events:
            top_score = max(m["score"] for m in memories) if memories else 0.0
            emit_event(
                "mempalace-interject",
                "memory_surfaced",
                ok=True,
                preview=injection[:100] if injection else None,
                data={
                    "trigger": "tool_pre",
                    "memory_ids": [m["id"] for m in memories],
                    "top_score": top_score,
                    "judge_used": judge_used,
                },
                session_id=sid,
            )
            try:
                await self._bridge_emit(
                    "memory-mempalace:memory_surfaced",
                    {
                        "ok": True,
                        "preview": injection[:100] if injection else None,
                        "trigger": "tool_pre",
                        "memory_ids": [m["id"] for m in memories],
                        "top_score": top_score,
                        "judge_used": judge_used,
                    },
                )
            except Exception:
                pass

        return HookResult(
            action="inject_context",
            context_injection=injection,
            context_injection_role="system",
            ephemeral=True,
        )

    async def on_orchestrator_complete(
        self, event: str, data: dict[str, Any]
    ) -> HookResult:
        """Fire on orchestrator:complete — check for contradicting memories.

        Guard flag prevents infinite loops: if we already injected this turn,
        skip. The turn counter is incremented here.
        """
        self._turn += 1
        sid = data.get("session_id")

        if not self.orc_enabled:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "orchestrator_complete", "reason": "disabled"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "orchestrator_complete",
                            "reason": "disabled",
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        # Infinite loop guard: skip if we already injected this turn
        if self._injected_this_turn:
            self._injected_this_turn = False  # reset for next turn
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "orchestrator_complete", "reason": "guard_flag"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "orchestrator_complete",
                            "reason": "guard_flag",
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        # Extract the LLM's response text
        response = data.get("response", "") or data.get("content", "")
        if not response or len(response) < 50:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "orchestrator_complete", "reason": "too_short"},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "orchestrator_complete",
                            "reason": "too_short",
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        # Only check for contradictions — use a higher threshold
        (
            memories,
            should_inject,
            skip_reason,
            judge_used,
        ) = await self._retrieve_and_gate(response, event)
        if not should_inject:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={"trigger": "orchestrator_complete", "reason": skip_reason},
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "orchestrator_complete",
                            "reason": skip_reason,
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        # Extra filter: only inject if a memory explicitly contradicts the response
        # (heuristic: look for negation keywords in the memory vs. the response)
        contradiction_keywords = [
            "failed",
            "error",
            "don't",
            "avoid",
            "never",
            "broken",
            "deprecated",
            "removed",
            "changed",
            "wrong",
            "incorrect",
        ]
        contradicting = [
            m
            for m in memories
            if any(kw in m["text"].lower() for kw in contradiction_keywords)
        ]
        if not contradicting:
            if self.emit_events:
                emit_event(
                    "mempalace-interject",
                    "interject_skipped",
                    ok=False,
                    data={
                        "trigger": "orchestrator_complete",
                        "reason": "below_threshold",
                    },
                    session_id=sid,
                )
                try:
                    await self._bridge_emit(
                        "memory-mempalace:interject_skipped",
                        {
                            "ok": False,
                            "trigger": "orchestrator_complete",
                            "reason": "below_threshold",
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        injection = _format_injection(contradicting, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in contradicting])
        self._injected_this_turn = True

        if self.emit_events:
            top_score = max(m["score"] for m in contradicting) if contradicting else 0.0
            emit_event(
                "mempalace-interject",
                "memory_surfaced",
                ok=True,
                preview=injection[:100] if injection else None,
                data={
                    "trigger": "orchestrator_complete",
                    "memory_ids": [m["id"] for m in contradicting],
                    "top_score": top_score,
                    "judge_used": judge_used,
                },
                session_id=sid,
            )
            try:
                await self._bridge_emit(
                    "memory-mempalace:memory_surfaced",
                    {
                        "ok": True,
                        "preview": injection[:100] if injection else None,
                        "trigger": "orchestrator_complete",
                        "memory_ids": [m["id"] for m in contradicting],
                        "top_score": top_score,
                        "judge_used": judge_used,
                    },
                )
            except Exception:
                pass

        return HookResult(
            action="inject_context",
            context_injection=injection,
            context_injection_role="system",
            ephemeral=True,
        )


# ── Module mount ──────────────────────────────────────────────────────────────


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the interject hook into the Amplifier coordinator.

    Registers three separate handlers on prompt:submit, tool:pre, and
    orchestrator:complete, all at priority 20 (early, non-critical).

    Also registers a contributor for observability events and a cross-hook
    listener for memory-mempalace:briefing_assembled to populate _briefed_ids.
    """
    cfg = config or {}

    register_events(
        coordinator,
        "memory-mempalace-interject",
        ["memory-mempalace:memory_surfaced", "memory-mempalace:interject_skipped"],
    )

    bridge_emit = make_async_bridge(coordinator)

    # Instantiate the hook with the bridge_emit closure
    hook = MempalaceInterjectHook(cfg, bridge_emit=bridge_emit)

    # Cross-hook listener: update _briefed_ids when briefing_assembled fires
    async def _on_briefing_assembled(event: str, data: Any) -> HookResult:
        try:
            ids = data.get("drawer_ids", []) if data else []
            hook._briefed_ids.update(str(i) for i in ids if i)
        except Exception:
            pass
        return HookResult(action="continue")

    coordinator.hooks.register(
        "memory-mempalace:briefing_assembled",
        _on_briefing_assembled,
        name="interject-briefing-listener",
    )

    # Register each event with its own dedicated handler method
    # Priority 20: runs early (after critical instrumentation at 50+)
    coordinator.hooks.register(
        HookRegistry.PROMPT_SUBMIT,
        hook.on_prompt_submit,
        priority=20,
        name="mempalace-interject-prompt",
    )
    coordinator.hooks.register(
        HookRegistry.TOOL_PRE,
        hook.on_tool_pre,
        priority=20,
        name="mempalace-interject-tool-pre",
    )
    coordinator.hooks.register(
        HookRegistry.ORCHESTRATOR_COMPLETE,
        hook.on_orchestrator_complete,
        priority=20,
        name="mempalace-interject-orc-complete",
    )

    return {
        "name": "hooks-mempalace-interject",
        "version": "1.1.0",
        "description": (
            "OR-firing memory interjection hook: surfaces relevant memories "
            "on prompt:submit, tool:pre, and orchestrator:complete"
        ),
        "config": {
            "cosine_threshold": hook.cosine_threshold,
            "uncertain_band": hook.uncertain_band,
            "max_inject_chars": hook.max_inject_chars,
            "cooldown_turns": hook.cooldown_turns,
            "prompt_enabled": hook.prompt_enabled,
            "tool_pre_enabled": hook.tool_pre_enabled,
            "orc_enabled": hook.orc_enabled,
            "llm_judge_enabled": hook.llm_judge_enabled,
            "emit_events": hook.emit_events,
        },
    }
