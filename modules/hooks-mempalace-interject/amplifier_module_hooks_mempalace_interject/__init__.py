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

Configuration keys (all optional):
  cosine_threshold: float = 0.72   # minimum similarity to inject
  uncertain_band:   float = 0.10   # band above threshold that triggers LLM judge
  max_inject_chars: int   = 800    # max chars per injection
  cooldown_turns:   int   = 3      # min turns between injections for same memory
  tool_pre_enabled: bool  = True   # enable tool:pre handler
  prompt_enabled:   bool  = True   # enable prompt:submit handler
  orc_enabled:      bool  = True   # enable orchestrator:complete handler
  palace_collection: str  = "mempalace_default"  # ChromaDB collection name
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import defaultdict
from pathlib import Path
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

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_COSINE_THRESHOLD = 0.72
DEFAULT_UNCERTAIN_BAND = 0.10
DEFAULT_MAX_INJECT_CHARS = 800
DEFAULT_COOLDOWN_TURNS = 3
DEFAULT_COLLECTION = "mempalace_default"

# ── Embedding helper ──────────────────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    """Embed text using the OpenAI embeddings API (text-embedding-3-small).

    Falls back to a zero vector on error so the hook never crashes the session.
    """
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI()
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],  # truncate to avoid token limit
        )
        return resp.data[0].embedding
    except Exception:
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── LLM judge ─────────────────────────────────────────────────────────────────

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
        score_str = resp.choices[0].message.content.strip()
        return min(1.0, max(0.0, float(score_str)))
    except Exception:
        return 0.0


# ── ChromaDB retrieval ────────────────────────────────────────────────────────

def _retrieve_memories(
    query_embedding: list[float],
    collection_name: str,
    n_results: int = 3,
) -> list[dict[str, Any]]:
    """Query ChromaDB for the top-n most similar memories.

    Returns a list of dicts with keys: id, text, score, metadata.
    """
    if not query_embedding:
        return []
    try:
        import chromadb  # type: ignore
        palace_dir = Path.home() / ".mempalace" / "chroma"
        client = chromadb.PersistentClient(path=str(palace_dir))
        try:
            collection = client.get_collection(collection_name)
        except Exception:
            return []  # collection doesn't exist yet
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "distances", "metadatas"],
        )
        memories = []
        docs = results.get("documents", [[]])[0]
        dists = results.get("distances", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        ids = results.get("ids", [[]])[0]
        for i, doc in enumerate(docs):
            # ChromaDB returns L2 distance; convert to cosine-like score
            # For normalized embeddings: cosine_sim ≈ 1 - (dist² / 2)
            dist = dists[i] if i < len(dists) else 1.0
            score = max(0.0, 1.0 - dist / 2.0)
            memories.append({
                "id": ids[i] if i < len(ids) else f"mem_{i}",
                "text": doc,
                "score": score,
                "metadata": metas[i] if i < len(metas) else {},
            })
        return memories
    except Exception:
        return []


# ── Injection formatter ───────────────────────────────────────────────────────

def _format_injection(memories: list[dict[str, Any]], event: str, max_chars: int) -> str:
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


# ── Main hook class ───────────────────────────────────────────────────────────

class MempalaceInterjectHook:
    """OR-firing hook that injects relevant memories at the right moment.

    Registers on prompt:submit, tool:pre, and orchestrator:complete.
    Uses cosine similarity as the primary relevance gate, with an optional
    LLM judge for scores in the uncertain band.
    """

    def __init__(self, config: dict[str, Any]) -> None:
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
        self.collection: str = config.get("palace_collection", DEFAULT_COLLECTION)
        self.prompt_enabled: bool = bool(config.get("prompt_enabled", True))
        self.tool_pre_enabled: bool = bool(config.get("tool_pre_enabled", True))
        self.orc_enabled: bool = bool(config.get("orc_enabled", True))

        # Per-turn guard flag: prevents re-injection in the same orchestrator run
        self._injected_this_turn: bool = False
        # Cooldown tracker: memory_id → last injection turn number
        self._last_injected: dict[str, int] = {}
        # Turn counter (incremented on orchestrator:complete)
        self._turn: int = 0
        # Briefing memory IDs (populated at session:start if briefing hook shares state)
        self._briefed_ids: set[str] = set()

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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Retrieve memories and decide whether to inject.

        Returns (memories_to_inject, should_inject).
        """
        # Embed the query
        query_embedding = await asyncio.get_event_loop().run_in_executor(
            None, _embed, query
        )
        if not query_embedding:
            return [], False

        # Retrieve top candidates
        candidates = _retrieve_memories(query_embedding, self.collection, n_results=5)
        if not candidates:
            return [], False

        # Filter out already-briefed and on-cooldown memories
        candidates = [
            m for m in candidates
            if m["id"] not in self._briefed_ids and not self._is_on_cooldown(m["id"])
        ]
        if not candidates:
            return [], False

        # Primary gate: cosine similarity threshold
        above_threshold = [m for m in candidates if m["score"] >= self.cosine_threshold]
        uncertain = [
            m for m in candidates
            if self.cosine_threshold - self.uncertain_band <= m["score"] < self.cosine_threshold
        ]

        # Secondary gate: LLM judge for uncertain-band memories
        if uncertain:
            judge_tasks = [_llm_judge(query, m["text"]) for m in uncertain]
            judge_scores = await asyncio.gather(*judge_tasks)
            for mem, js in zip(uncertain, judge_scores):
                if js >= 0.7:  # LLM judge threshold
                    above_threshold.append(mem)

        if not above_threshold:
            return [], False

        # Take top 2 by score
        top = sorted(above_threshold, key=lambda m: m["score"], reverse=True)[:2]
        return top, True

    # ── Event handlers ─────────────────────────────────────────────────────────

    async def on_prompt_submit(
        self, event: str, data: dict[str, Any]
    ) -> HookResult:
        """Fire on prompt:submit — inject before the LLM sees the user's prompt."""
        if not self.prompt_enabled:
            return HookResult(action="continue")

        prompt_text = data.get("prompt", "") or data.get("content", "")
        if not prompt_text or len(prompt_text) < 20:
            return HookResult(action="continue")

        # Reset per-turn guard (new user prompt = new turn)
        self._injected_this_turn = False

        memories, should_inject = await self._retrieve_and_gate(prompt_text, event)
        if not should_inject:
            return HookResult(action="continue")

        injection = _format_injection(memories, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in memories])
        self._injected_this_turn = True

        return HookResult(
            action="inject_context",
            context_injection=injection,
            context_injection_role="system",
            ephemeral=True,  # safe default: don't persist in conversation history
        )

    async def on_tool_pre(
        self, event: str, data: dict[str, Any]
    ) -> HookResult:
        """Fire on tool:pre — surface prior results for the same tool+input."""
        if not self.tool_pre_enabled:
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
            return HookResult(action="continue")

        memories, should_inject = await self._retrieve_and_gate(query, event)
        if not should_inject:
            return HookResult(action="continue")

        injection = _format_injection(memories, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in memories])

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

        if not self.orc_enabled:
            return HookResult(action="continue")

        # Infinite loop guard: skip if we already injected this turn
        if self._injected_this_turn:
            self._injected_this_turn = False  # reset for next turn
            return HookResult(action="continue")

        # Extract the LLM's response text
        response = data.get("response", "") or data.get("content", "")
        if not response or len(response) < 50:
            return HookResult(action="continue")

        # Only check for contradictions — use a higher threshold
        memories, should_inject = await self._retrieve_and_gate(response, event)
        if not should_inject:
            return HookResult(action="continue")

        # Extra filter: only inject if a memory explicitly contradicts the response
        # (heuristic: look for negation keywords in the memory vs. the response)
        contradiction_keywords = [
            "failed", "error", "don't", "avoid", "never", "broken",
            "deprecated", "removed", "changed", "wrong", "incorrect",
        ]
        contradicting = [
            m for m in memories
            if any(kw in m["text"].lower() for kw in contradiction_keywords)
        ]
        if not contradicting:
            return HookResult(action="continue")

        injection = _format_injection(contradicting, event, self.max_inject_chars)
        self._mark_injected([m["id"] for m in contradicting])
        self._injected_this_turn = True

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
    """
    cfg = config or {}
    hook = MempalaceInterjectHook(cfg)

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
        "version": "1.0.0",
        "description": (
            "OR-firing memory interjection hook: surfaces relevant memories "
            "on prompt:submit, tool:pre, and orchestrator:complete"
        ),
        "config": {
            "cosine_threshold": hook.cosine_threshold,
            "uncertain_band": hook.uncertain_band,
            "max_inject_chars": hook.max_inject_chars,
            "cooldown_turns": hook.cooldown_turns,
            "collection": hook.collection,
            "prompt_enabled": hook.prompt_enabled,
            "tool_pre_enabled": hook.tool_pre_enabled,
            "orc_enabled": hook.orc_enabled,
        },
    }
