"""``max_inject_chars`` is a real cap on the interject injection.

Regression: once the first snippet had been truncated to the cap, the room
left for the next one went negative and ``snippet[:negative]`` kept almost
the whole next memory -- measured as a 7,067-char "800-char" injection on a
real store.
"""

from __future__ import annotations

import amplifier_module_hooks_memory_interject as interject
from amplifier_core import HookRegistry  # type: ignore[import]


def _mem(text: str) -> dict:
    return {"id": text[:8], "text": text, "score": 0.9, "metadata": {}}


def test_long_memories_stay_within_cap() -> None:
    out = interject._format_injection(
        [_mem("a" * 5000), _mem("b" * 6000)], HookRegistry.PROMPT_SUBMIT, 800
    )
    assert len(out) <= 800 + 16  # cap + the "\n---\n" separator and ellipsis
    assert "b" * 50 not in out


def test_short_memories_unchanged() -> None:
    out = interject._format_injection(
        [_mem("first memory"), _mem("second memory")], HookRegistry.PROMPT_SUBMIT, 800
    )
    assert "first memory" in out and "second memory" in out
    assert "…" not in out


def test_second_memory_truncated_when_room_remains() -> None:
    out = interject._format_injection(
        [_mem("x" * 100), _mem("y" * 2000)], HookRegistry.PROMPT_SUBMIT, 800
    )
    assert "x" * 100 in out and out.endswith("…")
    assert len(out) <= 800 + 16


def test_cap_is_exact_including_separators() -> None:
    """Review optional 7: separators/join newlines count against the cap."""
    import random

    rng = random.Random(7)
    for cap in (100, 300, 800, 2000):
        for _ in range(300):
            mems = [
                _mem("m" * rng.randint(1, 3000)) for _ in range(rng.randint(1, 6))
            ]
            out = interject._format_injection(mems, HookRegistry.PROMPT_SUBMIT, cap)
            assert len(out) <= cap, (cap, len(out))
