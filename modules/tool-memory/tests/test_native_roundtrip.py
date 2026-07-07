"""
KG-N1 (docs/plans/2026-07-07-native-cutover-design.md \u00a712): the killer gate
assigned to B2.

Remember->search round-trip through the REAL tool surface:
``MemoryTool.execute({"operation": "remember", ...})`` returns success=True
with a ref; a SECOND ``execute({"operation": "search", ...})`` call returns
the content verbatim with a score above threshold -- via the auto-started
native memory daemon and the real local embedder (fastembed), with NO
mocking of the transport layer at all.

Skipped when amplifier-data is not importable (no substrate available).
Uses a scratch ``AMPLIFIER_MEMORY_HOME`` so this test never touches a real
user's memory home, and shuts the daemon down afterward.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory import MemoryTool  # noqa: E402
from amplifier_module_tool_memory.client import ensure_daemon  # noqa: E402


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestKGN1RememberSearchRoundTrip:
    def test_execute_remember_then_execute_search_recalls_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "memory-home"
        monkeypatch.setenv("AMPLIFIER_MEMORY_HOME", str(home))

        tool = MemoryTool()
        content = "KG-N1 round-trip probe: the native cutover keeps memory alive"

        # Wait for the daemon's embedder to actually warm up before filing --
        # fastembed's model is already cached locally in this environment
        # (verified manually), so a real, non-degraded round-trip is the
        # honestly-achievable outcome here, not just the documented degraded
        # carve-out. Poll /health via the client directly (this also
        # auto-starts the daemon, per ensure_daemon()'s \u00a75.2 contract).
        warm_deadline = time.monotonic() + 30.0
        embedder_ready = False
        while time.monotonic() < warm_deadline:
            probe_client = ensure_daemon(home)
            if probe_client is not None:
                hc = probe_client.health()
                if hc is not None and hc.get("embedder", {}).get("ready"):
                    embedder_ready = True
                    break
            time.sleep(0.5)

        remember_result = _run(
            tool.execute(
                {
                    "operation": "remember",
                    "wing": "wing_kg_n1",
                    "room": "roundtrip",
                    "content": content,
                }
            )
        )
        assert remember_result.success, remember_result.error
        remember_payload = json.loads(remember_result.output)
        assert remember_payload.get("ref"), "remember must return a real ref"

        # A SECOND execute() call -- a fresh MemoryTool instance, simulating
        # a second process/session -- discovers the SAME auto-started daemon
        # via ensure_daemon()'s daemon.json discovery (\u00a75.2), not a second
        # spawn.
        tool2 = MemoryTool()
        search_payload: dict[str, Any] = {}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            search_result = _run(
                tool2.execute(
                    {
                        "operation": "search",
                        "query": "native cutover memory alive",
                        "wing": "wing_kg_n1",
                        "limit": 5,
                    }
                )
            )
            assert search_result.success, search_result.error
            search_payload = json.loads(search_result.output)
            if search_payload.get("results"):
                break
            time.sleep(0.5)

        results = search_payload.get("results", [])
        assert results, f"expected at least one search hit, got {search_payload}"
        assert any(content in r.get("content", "") for r in results), (
            f"expected verbatim recall of the remembered content, got {results}"
        )
        top_score = results[0].get("score", 0.0)
        assert top_score > 0.3, (
            f"expected a meaningfully high top score, got {top_score}"
        )

        # Honest reporting: record whether this ran with the real embedder or
        # degraded to lexical-only -- never claim semantic recall silently
        # ran in lexical mode.
        degraded = search_payload.get("degraded")
        print(
            f"KG-N1: embedder_ready_before_remember={embedder_ready!r} "
            f"degraded={degraded!r} top_score={top_score!r}"
        )
        if embedder_ready:
            # The embedder was confirmed warm before we ever filed the
            # drawer -- a lexical-only degrade at this point would be a
            # real regression, not an environment limitation.
            assert degraded is None, (
                "embedder was ready before remember(), but search still "
                f"reported degraded={degraded!r} -- real-embedder round-trip failed"
            )

        # Clean up the auto-started daemon.
        client = ensure_daemon(home)
        if client is not None:
            client.shutdown()
