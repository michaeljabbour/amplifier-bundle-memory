"""
amplifier-module-context-sleep

Sleep-time context consolidation for Amplifier sessions.

Two-buffer architecture
-----------------------
  _raw     — append-only list of ALL messages.  get_messages() returns a copy
             of THIS buffer.  NON-DESTRUCTIVE INVARIANT: never modified by
             compaction.

  _working — live window for LLM requests: [consolidated-memory system msg?]
             + recent verbatim messages (up to keep_recent_messages).
             get_messages_for_request() returns this cheaply.

Compaction trigger
------------------
  add_message() calls compact() when:
    enabled=True  AND  _estimate_tokens(_working) > consolidation_threshold_tokens

One compact() pass
------------------
  1. Identify evicted = _working[:-keep_recent] (excl. any prior memory msg).
  2. Call LLM provider with FAITHFUL prompt (or CREATIVE with loud warning).
  3. If provider unavailable / call fails → verbatim deduplication fallback
     (no data ever silently lost).
  4. Replace evicted slice with one system msg {"[Consolidated memory]\\n<note>}.
  5. Emit context:pre_compact, context:post_compact, context:sleep_complete.

Single-pass design is intentional: H3 (multiple passes give no measurable
benefit) was a null result in the empirical study.  See README.md.

Empirical guardrails (do not change without re-running the study)
-----------------------------------------------------------------
  • Faithful LLM consolidation ≈ verbatim retention for atomic facts —
    value is compressing REDUNDANT / verbose context, not improving reasoning.
  • Creative / reorganise mode HURTS (~−28 pp via fabrication); default=faithful.
  • Natural-language prose beats symbolic triples (~+19 pp).
  • Single pass is optimal.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful amplifier_core import — matches sibling modules exactly.
# The entire module works without amplifier_core (tests, standalone use).
# ---------------------------------------------------------------------------
try:
    from amplifier_core import HookResult as _HookResult  # type: ignore  # noqa: F401

    _AMPLIFIER_CORE_AVAILABLE = True
except ImportError:
    _AMPLIFIER_CORE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Token estimation: tiktoken cl100k when available, else char/4 fallback
# ---------------------------------------------------------------------------
try:
    import tiktoken as _tiktoken  # type: ignore

    _ENC = _tiktoken.get_encoding("cl100k_base")
    _HAS_TIKTOKEN = True
except Exception:
    _ENC = None  # type: ignore
    _HAS_TIKTOKEN = False


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Approximate token count for a list of messages."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content") or ""
                    if t:
                        parts.append(str(t))
    combined = " ".join(parts)
    if _HAS_TIKTOKEN and _ENC is not None:
        try:
            return len(_ENC.encode(combined))
        except Exception:
            pass
    return max(1, len(combined) // 4)


# ---------------------------------------------------------------------------
# Research-validated consolidation prompts
# ---------------------------------------------------------------------------

# FAITHFUL (default) — strict anti-hallucination guardrails.
_FAITHFUL_SYSTEM = (
    "You are an offline memory-consolidation process. Compress the conversation/"
    "context below into a compact note. STRICT RULES: "
    "(1) NEVER invent, infer, or derive any fact, name, number, decision, or "
    "relationship. "
    "(2) Preserve every concrete fact verbatim where possible; you may drop pure "
    "redundancy and filler. "
    "(3) Keep readable natural-language prose (NOT terse symbol/triple notation). "
    "(4) If unsure whether something matters, KEEP it. "
    "Output only the consolidated note."
)

# CREATIVE — empirically fabricates ~50-75 % of facts.  Research use ONLY.
_CREATIVE_WARNING = (
    "\u26a0  context-sleep WARNING: 'creative' consolidation style is EMPIRICALLY "
    "SHOWN to fabricate ~50-75 % of facts in controlled studies.  This mode is "
    "for RESEARCH COMPARISON ONLY and must NEVER be used in production sessions "
    "where factual accuracy is required."
)

_CREATIVE_SYSTEM = (
    "You are an offline memory-consolidation process. Reorganise and synthesise "
    "the conversation/context below into a compact, well-structured note. You "
    "may derive higher-level insights and restructure information for clarity. "
    "Output only the consolidated note."
)


# ---------------------------------------------------------------------------
# Message → text helper
# ---------------------------------------------------------------------------

def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """Serialise messages to a readable text block for the consolidation prompt."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if isinstance(content, list):
            bits: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content") or ""
                    if t:
                        bits.append(str(t))
            content = " ".join(bits)
        parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM consolidation call (fully defensive — gracefully degrades to None)
# ---------------------------------------------------------------------------

async def _call_provider(
    provider: Any,
    text: str,
    style: str,
) -> str | None:
    """Attempt one LLM consolidation call.

    Returns the consolidated note on success, None on any failure.
    Caller is responsible for applying the verbatim fallback when None.
    """
    if style == "creative":
        logger.warning("[context-sleep] %s", _CREATIVE_WARNING)
        system_prompt = _CREATIVE_SYSTEM
    else:
        system_prompt = _FAITHFUL_SYSTEM

    # Import ChatRequest — degrade gracefully if amplifier_core absent.
    try:
        from amplifier_core.message_models import ChatRequest  # type: ignore
    except ImportError:
        logger.warning(
            "[context-sleep] amplifier_core.message_models unavailable; "
            "using verbatim fallback"
        )
        return None

    # Build request.  Try Format A (string content, most portable) then
    # Format B (list-of-text-blocks for stricter Pydantic schemas).
    request = None
    for msg_list in (
        [  # Format A
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        [  # Format B
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
    ):
        try:
            request = ChatRequest(messages=msg_list, max_tokens=2048)
            break
        except Exception:
            continue

    if request is None:
        logger.warning(
            "[context-sleep] Could not construct ChatRequest; "
            "using verbatim fallback"
        )
        return None

    try:
        response = await provider.complete(request)
        # Extract text from response — compatible with real ChatResponse and test fakes.
        if hasattr(response, "content") and response.content:
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    return str(block.text)
        if hasattr(response, "output") and response.output:
            return str(response.output)
        return None
    except Exception as exc:
        logger.warning("[context-sleep] Provider consolidation call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Non-LLM faithful fallback
# ---------------------------------------------------------------------------

def _verbatim_fallback(text: str) -> str:
    """Faithful retention without LLM: remove only exact duplicate lines.

    CONTRACT: no information is silently dropped.  Every unique line is kept.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = line.strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# SleepConsolidatingContext
# ---------------------------------------------------------------------------

_MEMORY_PREFIX = "[Consolidated memory]"


class SleepConsolidatingContext:
    """ContextManager with sleep-time LLM consolidation.

    Config keys (all optional, with defaults):
      consolidation_threshold_tokens  int   8000
      keep_recent_messages            int   20
      consolidation_provider          str   None  (picks first available)
      style                           str   "faithful"
      enabled                         bool  True

    Provider and hooks are injected by on_session_ready(); the module
    degrades to verbatim fallback until then.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._threshold: int = int(cfg.get("consolidation_threshold_tokens", 8000))
        self._keep_recent: int = int(cfg.get("keep_recent_messages", 20))
        self._style: str = str(cfg.get("style", "faithful"))
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._provider_name: str | None = cfg.get("consolidation_provider")

        # Two buffers
        self._raw: list[dict[str, Any]] = []
        self._working: list[dict[str, Any]] = []

        # Set in on_session_ready(); None until then.
        self._provider: Any = None
        self._hooks: Any = None

    # ------------------------------------------------------------------
    # ContextManager protocol (all async — hard contract requirement)
    # ------------------------------------------------------------------

    async def add_message(self, message: dict[str, Any]) -> None:
        """Append to both buffers; compact if threshold exceeded."""
        self._raw.append(message)
        self._working.append(message)
        if self._enabled and await self.should_compact():
            await self.compact()

    async def get_messages_for_request(
        self,
        token_budget: int | None = None,  # noqa: ARG002
        provider: Any | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Return the live working window (cheap — compaction already done)."""
        return list(self._working)

    async def get_messages(self) -> list[dict[str, Any]]:
        """Return the full raw history.

        NON-DESTRUCTIVE INVARIANT: this list is NEVER modified by compaction.
        Always returns every message passed to add_message() since last
        set_messages()/clear().
        """
        return list(self._raw)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Set messages directly (session resume)."""
        self._raw = list(messages)
        self._working = list(messages)

    async def clear(self) -> None:
        """Clear both buffers."""
        self._raw = []
        self._working = []

    async def should_compact(self) -> bool:
        """True when the working window exceeds the token threshold."""
        return _estimate_tokens(self._working) > self._threshold

    async def compact(self) -> None:
        """One sleep-time consolidation pass.

        Single-pass design: H3 (multiple passes give no measurable benefit)
        was a null result in the empirical study.
        """
        working = list(self._working)

        # Detect an existing consolidated-memory system message at position 0.
        has_mem = (
            bool(working)
            and working[0].get("role") == "system"
            and isinstance(working[0].get("content"), str)
            and working[0]["content"].startswith(_MEMORY_PREFIX)
        )

        if has_mem:
            prior_note: str | None = working[0]["content"][len(_MEMORY_PREFIX) + 1:]
            verbatim = working[1:]
        else:
            prior_note = None
            verbatim = working

        # --- pre_compact event ---
        token_count = _estimate_tokens(working)
        await self._emit("context:pre_compact", {
            "message_count": len(working),
            "token_count": token_count,
        })

        # Nothing to evict → emit post and return unchanged.
        if len(verbatim) <= self._keep_recent:
            await self._emit("context:post_compact", {
                "message_count": len(working),
                "token_count": token_count,
            })
            return

        evicted = verbatim[: -self._keep_recent]
        keep = verbatim[-self._keep_recent :]

        # --- build the text to consolidate ---
        sections: list[str] = []
        if prior_note:
            sections.append(f"[Prior consolidated note]\n{prior_note}")
        sections.append(_messages_to_text(evicted))
        consolidation_input = "\n\n".join(sections)

        # --- LLM consolidation or verbatim fallback (no data loss either way) ---
        note: str | None = None
        if self._provider is not None:
            note = await _call_provider(self._provider, consolidation_input, self._style)
            if note is None:
                logger.warning(
                    "[context-sleep] LLM call returned None; using verbatim fallback"
                )
        if note is None:
            note = _verbatim_fallback(consolidation_input)

        # --- rebuild _working ---
        new_mem: dict[str, Any] = {
            "role": "system",
            "content": f"{_MEMORY_PREFIX}\n{note}",
        }
        self._working = [new_mem] + list(keep)

        # --- post_compact + sleep_complete events ---
        new_tokens = _estimate_tokens(self._working)
        await self._emit("context:post_compact", {
            "message_count": len(self._working),
            "token_count": new_tokens,
        })
        await self._emit("context:sleep_complete", {
            "note": note,
            "evicted_count": len(evicted),
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        """Emit a hook event; silently suppress all errors."""
        if self._hooks is None:
            return
        try:
            await self._hooks.emit(event, data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level entry points (Amplifier kernel protocol)
# ---------------------------------------------------------------------------

async def mount(
    coordinator: Any,
    config: dict[str, Any] | None = None,
) -> Any:
    """Mount context-sleep into the Amplifier session.

    Creates a SleepConsolidatingContext, mounts it at the "session"/"context"
    mount point, and returns an async cleanup callable.

    Provider and hooks are not yet available at mount time; they are wired
    in on_session_ready() once full composition is complete.
    """
    ctx = SleepConsolidatingContext(config)
    await coordinator.mount("session", ctx, name="context")

    async def _cleanup() -> None:
        await ctx.clear()

    return _cleanup


async def on_session_ready(coordinator: Any) -> None:
    """Wire provider and hooks after full session composition.

    Called once per session after all modules across all phases have
    completed mount().  Follows the canonical on_session_ready pattern
    for provider capture (see amplifier-bundle-context-managed).

    Safe when no providers are mounted — the module degrades gracefully
    to verbatim fallback inside compact().
    """
    # Retrieve the context we mounted earlier.
    try:
        session: dict[str, Any] = coordinator.get("session") or {}
        ctx = session.get("context")
        if not isinstance(ctx, SleepConsolidatingContext):
            return
    except Exception:
        return

    # Wire the hook registry for event emission.
    try:
        ctx._hooks = coordinator.hooks
    except Exception:
        pass

    # Wire the consolidation provider: use configured name, or pick first.
    try:
        providers: dict[str, Any] = coordinator.get("providers") or {}
        if providers:
            name = ctx._provider_name
            if name and name in providers:
                ctx._provider = providers[name]
            else:
                ctx._provider = next(iter(providers.values()))
    except Exception:
        pass
