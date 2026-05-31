"""
Behaviour and contract tests for amplifier-module-context-sleep.

No real LLM calls are made — a _FakeProvider returns a fixed note.
The _FakeCoordinator mirrors the style from tests/test_contract.py
but adds get() and async mount() so on_session_ready() works correctly.

Test coverage:
  1. mount() registers the context under ("session", "context").
  2. on_session_ready() wires the provider and hooks.
  3. All ContextManager protocol methods exist and are async.
  4. add_message() below threshold does NOT compact.
  5. compact() shrinks _working while get_messages() returns FULL raw history.
  6. The LLM note appears in the consolidated-memory message.
  7. Auto-compact triggers when threshold is crossed via add_message().
  8. provider=None fallback retains evicted text (no data loss).
  9. set_messages() and clear() work correctly.
 10. should_compact() reflects token state.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

# Skip the whole file if amplifier_core is not installed — consistent with
# test_contract.py which uses the same guard.
amplifier_core = pytest.importorskip("amplifier_core")

import amplifier_module_context_sleep as m  # noqa: E402 (after importorskip)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _FakeBlock:
    """Minimal content block understood by _call_provider()'s response parser."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    """Minimal provider response understood by _call_provider()."""

    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeProvider:
    """Fake provider: always returns a fixed note, never hits a real LLM."""

    name = "fake-provider"
    FIXED_NOTE = "Consolidated: key facts preserved verbatim. (fake note)"

    async def complete(self, request: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self.FIXED_NOTE)


class _FakeCoordinator:
    """Minimal coordinator stub — mirrors _FakeCoordinator from test_contract.py
    but adds get() and async mount() needed by context-sleep.
    """

    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        from amplifier_core import HookRegistry

        self.hooks = HookRegistry()
        self.session_id: str = "test-session"
        self._mounted: dict[str, dict[str, Any]] = {}
        self._contributors: dict[str, dict[str, Any]] = {}
        if providers:
            self._mounted["providers"] = providers

    async def mount(
        self, mount_point: str, obj: Any, *, name: str = "unnamed"
    ) -> None:
        """Register obj at mount_point under name."""
        if mount_point not in self._mounted:
            self._mounted[mount_point] = {}
        self._mounted[mount_point][name] = obj

    def get(self, mount_point: str) -> dict[str, Any] | None:
        """Return the dict of modules at mount_point (or None)."""
        return self._mounted.get(mount_point)

    def register_contributor(
        self, channel: str, name: str, callback: Any
    ) -> None:
        self._contributors.setdefault(channel, {})[name] = callback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(i: int, extra: str = "") -> dict[str, Any]:
    return {"role": "user", "content": f"Message {i}: {extra}"}


def _long_msg(i: int, chars: int = 400) -> dict[str, Any]:
    """A message long enough to noticeably affect the token count."""
    return {"role": "user", "content": f"Message {i}: " + "word " * (chars // 5)}


# ---------------------------------------------------------------------------
# 1. mount() and protocol shape
# ---------------------------------------------------------------------------


class TestMountAndProtocol:
    async def test_mount_registers_context(self) -> None:
        """mount() must mount the context at ("session", "context")."""
        coord = _FakeCoordinator()
        cleanup = await m.mount(coord, {})

        session = coord.get("session") or {}
        ctx = session.get("context")
        assert isinstance(ctx, m.SleepConsolidatingContext), (
            "mount() must register a SleepConsolidatingContext at name='context'"
        )
        # cleanup must be callable (async)
        assert callable(cleanup), "mount() must return a cleanup callable"

    async def test_mount_returns_async_cleanup(self) -> None:
        coord = _FakeCoordinator()
        cleanup = await m.mount(coord, {})
        assert inspect.iscoroutinefunction(cleanup), "cleanup must be async"

    def test_all_protocol_methods_exist_and_are_async(self) -> None:
        """Every ContextManager protocol method must exist and be a coroutine function."""
        ctx = m.SleepConsolidatingContext()
        required = [
            "add_message",
            "get_messages_for_request",
            "get_messages",
            "set_messages",
            "clear",
            "should_compact",
            "compact",
        ]
        for name in required:
            method = getattr(ctx, name, None)
            assert method is not None, f"SleepConsolidatingContext missing '{name}'"
            assert inspect.iscoroutinefunction(method), f"'{name}' must be async"

    def test_mount_is_async_and_has_correct_signature(self) -> None:
        """mount(coordinator, config=None) must be async with ≥2 params."""
        assert inspect.iscoroutinefunction(m.mount), "mount must be async"
        sig = inspect.signature(m.mount)
        params = list(sig.parameters)
        assert len(params) >= 2, "mount must accept (coordinator, config)"

    def test_on_session_ready_is_async(self) -> None:
        assert inspect.iscoroutinefunction(m.on_session_ready), (
            "on_session_ready must be async"
        )


# ---------------------------------------------------------------------------
# 2. on_session_ready() wires provider and hooks
# ---------------------------------------------------------------------------


class TestOnSessionReady:
    async def test_provider_wired(self) -> None:
        """on_session_ready() must capture the provider onto the context."""
        provider = _FakeProvider()
        coord = _FakeCoordinator(providers={"fake": provider})
        await m.mount(coord, {})
        await m.on_session_ready(coord)

        session = coord.get("session") or {}
        ctx = session.get("context")
        assert ctx._provider is provider, (
            "on_session_ready must wire the available provider"
        )

    async def test_hooks_wired(self) -> None:
        """on_session_ready() must capture the hook registry."""
        coord = _FakeCoordinator()
        await m.mount(coord, {})
        await m.on_session_ready(coord)

        session = coord.get("session") or {}
        ctx = session.get("context")
        assert ctx._hooks is coord.hooks, (
            "on_session_ready must wire coordinator.hooks"
        )

    async def test_no_provider_is_safe(self) -> None:
        """on_session_ready() with no providers must not raise."""
        coord = _FakeCoordinator(providers={})
        await m.mount(coord, {})
        await m.on_session_ready(coord)  # must not raise

        session = coord.get("session") or {}
        ctx = session.get("context")
        assert ctx._provider is None

    async def test_named_provider_selected(self) -> None:
        """consolidation_provider config must take precedence over first-pick."""
        p_a = _FakeProvider()
        p_a.name = "provider-a"
        p_b = _FakeProvider()
        p_b.name = "provider-b"
        coord = _FakeCoordinator(providers={"provider-a": p_a, "provider-b": p_b})
        await m.mount(coord, {"consolidation_provider": "provider-b"})
        await m.on_session_ready(coord)

        session = coord.get("session") or {}
        ctx = session.get("context")
        assert ctx._provider is p_b, (
            "consolidation_provider config key must select the named provider"
        )


# ---------------------------------------------------------------------------
# 3. Below-threshold: add_message must NOT compact
# ---------------------------------------------------------------------------


class TestBelowThreshold:
    async def test_no_compact_below_threshold(self) -> None:
        """With an impossibly high threshold, add_message must never compact."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 20,
        })
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
            {"role": "user", "content": "Goodbye"},
        ]
        for msg in msgs:
            await ctx.add_message(msg)

        raw = await ctx.get_messages()
        working = await ctx.get_messages_for_request()

        assert raw == msgs, "raw history must equal the added messages"
        assert working == msgs, "working window must equal the added messages"

    async def test_should_compact_false_below_threshold(self) -> None:
        ctx = m.SleepConsolidatingContext({"consolidation_threshold_tokens": 1_000_000})
        await ctx.add_message({"role": "user", "content": "tiny"})
        assert not await ctx.should_compact()


# ---------------------------------------------------------------------------
# 4 & 5. Direct compact(): shrinks _working, preserves _raw
# ---------------------------------------------------------------------------


class TestDirectCompact:
    async def test_compact_shrinks_working(self) -> None:
        """compact() must reduce _working when len(verbatim) > keep_recent."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,  # disable auto
            "keep_recent_messages": 2,
        })
        ctx._provider = _FakeProvider()

        msgs = [_make_msg(i, "content detail here") for i in range(6)]
        for msg in msgs:
            await ctx.add_message(msg)

        await ctx.compact()

        working = await ctx.get_messages_for_request()
        # Expected: [consolidated-memory msg] + last 2 verbatim = 3 total
        assert len(working) == 3, (
            f"Expected 3 messages in working after compact (got {len(working)})"
        )

    async def test_compact_preserves_raw_invariant(self) -> None:
        """get_messages() MUST return all original messages after compact()."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 2,
        })
        ctx._provider = _FakeProvider()

        msgs = [_make_msg(i) for i in range(6)]
        for msg in msgs:
            await ctx.add_message(msg)

        await ctx.compact()

        raw = await ctx.get_messages()
        assert len(raw) == 6, "Raw must preserve ALL 6 original messages"
        for orig, got in zip(msgs, raw):
            assert orig == got, "Raw messages must be byte-for-byte identical to originals"

    async def test_compact_creates_memory_message(self) -> None:
        """compact() must create a [Consolidated memory] system message at pos 0."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 2,
        })
        ctx._provider = _FakeProvider()

        for i in range(5):
            await ctx.add_message(_make_msg(i))

        await ctx.compact()

        working = await ctx.get_messages_for_request()
        assert working[0]["role"] == "system"
        assert working[0]["content"].startswith("[Consolidated memory]"), (
            "First working message must be the consolidated-memory system message"
        )

    async def test_compact_verbatim_tail_intact(self) -> None:
        """The last keep_recent messages must be verbatim in _working after compact."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 2,
        })
        ctx._provider = _FakeProvider()

        msgs = [_make_msg(i) for i in range(5)]
        for msg in msgs:
            await ctx.add_message(msg)

        await ctx.compact()

        working = await ctx.get_messages_for_request()
        # working = [mem_msg, msgs[3], msgs[4]]
        assert working[1] == msgs[3], "Second-to-last message must appear verbatim"
        assert working[2] == msgs[4], "Last message must appear verbatim"


# ---------------------------------------------------------------------------
# 6. Provider note appears in consolidated message
# ---------------------------------------------------------------------------


class TestProviderNote:
    async def test_fake_provider_note_in_memory_msg(self) -> None:
        """When a provider is available, its returned note must appear in _working[0]."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 1,
        })
        ctx._provider = _FakeProvider()

        for i in range(4):
            await ctx.add_message(_make_msg(i))

        await ctx.compact()

        working = await ctx.get_messages_for_request()
        mem_content = working[0]["content"]
        assert _FakeProvider.FIXED_NOTE in mem_content, (
            f"Provider note must appear in consolidated memory message.\n"
            f"Expected: {_FakeProvider.FIXED_NOTE!r}\n"
            f"Got content: {mem_content!r}"
        )


# ---------------------------------------------------------------------------
# 7. Auto-compact triggers through add_message()
# ---------------------------------------------------------------------------


class TestAutoCompact:
    async def test_auto_compact_triggered(self) -> None:
        """Crossing the token threshold via add_message() must trigger compact()."""
        # Very low threshold; each long message far exceeds it.
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 10,
            "keep_recent_messages": 1,
        })
        ctx._provider = _FakeProvider()

        # Each message: ~400 chars → ~100 tokens (char/4) or ~80 (tiktoken).
        # That alone exceeds threshold=10, so compact triggers after msg[1].
        for i in range(5):
            await ctx.add_message(_long_msg(i))

        raw = await ctx.get_messages()
        working = await ctx.get_messages_for_request()

        assert len(raw) == 5, "Raw must contain all 5 messages"
        assert len(working) < len(raw), (
            "_working must be shorter than _raw after auto-compact"
        )
        assert working[0]["content"].startswith("[Consolidated memory]"), (
            "First working message must be the consolidated-memory system message"
        )

    async def test_auto_compact_disabled(self) -> None:
        """enabled=False must prevent auto-compact regardless of token count."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1,  # immediately triggering if enabled
            "keep_recent_messages": 1,
            "enabled": False,
        })
        for i in range(5):
            await ctx.add_message(_long_msg(i))

        raw = await ctx.get_messages()
        working = await ctx.get_messages_for_request()
        assert len(raw) == len(working) == 5, (
            "With enabled=False, no compaction must occur"
        )


# ---------------------------------------------------------------------------
# 8. provider=None fallback: evicted text retained, no data loss
# ---------------------------------------------------------------------------


class TestNoProviderFallback:
    async def test_fallback_retains_evicted_content(self) -> None:
        """Verbatim fallback must embed evicted message content in memory msg."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 2,
        })
        assert ctx._provider is None, "No provider should be set in this test"

        facts = [
            f"Unique fact {i}: the answer is {i * 7}"
            for i in range(6)
        ]
        for fact in facts:
            await ctx.add_message({"role": "user", "content": fact})

        await ctx.compact()

        raw = await ctx.get_messages()
        working = await ctx.get_messages_for_request()

        # Raw must be fully intact
        assert len(raw) == 6, "Raw must preserve all 6 messages"

        # Evicted = msgs 0–3 (keep_recent=2 → keep msgs 4 & 5)
        mem_content = working[0]["content"]
        assert "[Consolidated memory]" in mem_content

        for i in range(4):  # msgs 0–3 were evicted
            assert facts[i] in mem_content, (
                f"Evicted content 'Unique fact {i}' must appear in memory message"
            )

    async def test_fallback_no_data_loss_on_provider_failure(self) -> None:
        """If provider.complete() raises, verbatim fallback must still run."""

        class _BrokenProvider:
            async def complete(self, request: Any, **kwargs: Any) -> Any:
                raise RuntimeError("simulated provider failure")

        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
            "keep_recent_messages": 1,
        })
        ctx._provider = _BrokenProvider()

        msgs = [{"role": "user", "content": f"critical data {i}"} for i in range(4)]
        for msg in msgs:
            await ctx.add_message(msg)

        # Must not raise even with a broken provider
        await ctx.compact()

        working = await ctx.get_messages_for_request()
        raw = await ctx.get_messages()

        assert len(raw) == 4, "Raw must be intact after provider failure"
        mem_content = working[0]["content"]
        assert "[Consolidated memory]" in mem_content
        # Evicted messages (msgs 0–2) must appear in the fallback note
        for i in range(3):
            assert f"critical data {i}" in mem_content, (
                f"'critical data {i}' must be in memory msg after fallback"
            )


# ---------------------------------------------------------------------------
# 9. set_messages() and clear()
# ---------------------------------------------------------------------------


class TestSetMessagesAndClear:
    async def test_set_messages_populates_both_buffers(self) -> None:
        ctx = m.SleepConsolidatingContext()
        msgs = [
            {"role": "user", "content": "Previous context line 1"},
            {"role": "assistant", "content": "Previous context line 2"},
        ]
        await ctx.set_messages(msgs)
        assert await ctx.get_messages() == msgs
        assert await ctx.get_messages_for_request() == msgs

    async def test_set_messages_replaces_existing(self) -> None:
        ctx = m.SleepConsolidatingContext()
        for i in range(3):
            await ctx.add_message(_make_msg(i))

        new_msgs = [{"role": "system", "content": "Fresh start"}]
        await ctx.set_messages(new_msgs)

        assert await ctx.get_messages() == new_msgs
        assert await ctx.get_messages_for_request() == new_msgs

    async def test_clear_empties_both_buffers(self) -> None:
        ctx = m.SleepConsolidatingContext()
        for i in range(4):
            await ctx.add_message(_make_msg(i))

        await ctx.clear()

        assert await ctx.get_messages() == []
        assert await ctx.get_messages_for_request() == []

    async def test_add_after_clear(self) -> None:
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
        })
        for i in range(3):
            await ctx.add_message(_make_msg(i))

        await ctx.clear()
        new_msg = {"role": "user", "content": "Post-clear message"}
        await ctx.add_message(new_msg)

        assert await ctx.get_messages() == [new_msg]
        assert await ctx.get_messages_for_request() == [new_msg]


# ---------------------------------------------------------------------------
# 10. should_compact() reflects token state
# ---------------------------------------------------------------------------


class TestShouldCompact:
    async def test_should_compact_true_when_above_threshold(self) -> None:
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 10,
            "enabled": True,
        })
        # Add a message with ~100 chars → ~25 tokens (char/4) > threshold=10
        await ctx.add_message({"role": "user", "content": "x" * 100})
        # Note: add_message may have already compacted (keep_recent default=20,
        # but with only 1 message there's nothing to evict).
        # Force _working to have a long message to check should_compact():
        ctx._working = [{"role": "user", "content": "y" * 200}]
        assert await ctx.should_compact(), (
            "should_compact must return True when token count > threshold"
        )

    async def test_should_compact_false_when_below_threshold(self) -> None:
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 1_000_000,
        })
        await ctx.add_message({"role": "user", "content": "tiny"})
        assert not await ctx.should_compact()


# ---------------------------------------------------------------------------
# 11. Multiple compactions preserve raw invariant (regression test)
# ---------------------------------------------------------------------------


class TestMultipleCompactions:
    async def test_multiple_compactions_raw_invariant(self) -> None:
        """After several compaction cycles, raw must still hold every message."""
        ctx = m.SleepConsolidatingContext({
            "consolidation_threshold_tokens": 10,
            "keep_recent_messages": 1,
        })
        ctx._provider = _FakeProvider()

        all_msgs = []
        for i in range(10):
            msg = _long_msg(i)
            all_msgs.append(msg)
            await ctx.add_message(msg)

        raw = await ctx.get_messages()
        assert len(raw) == 10, "All 10 messages must be in raw after repeated compaction"
        for orig, got in zip(all_msgs, raw):
            assert orig == got, f"Message at index {all_msgs.index(orig)} was mutated"
