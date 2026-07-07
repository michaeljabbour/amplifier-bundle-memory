"""
Native memory e2e smoke (DTU-gated, docs/plans/2026-07-07-native-cutover-design.md
\u00a710.3 / KG-N1).

Exercises the completed native stack end-to-end as a user would, inside the
memory-native-e2e DTU container: the friend-scenario -- one process files a
drawer via ``MemoryTool.execute({"operation": "remember", ...})``, a SECOND
process recalls it via ``execute({"operation": "search", ...})`` through the
auto-started memory daemon and the real local embedder. There is no vendor
subprocess or ChromaDB anywhere in this path -- the daemon (amplifier-data +
FastEmbedEmbedder) is the ONE store.

Skipped outside the DTU container (see tests/integration/conftest.py's
pytest_collection_modifyitems -- the same sentinel gate every test in this
directory uses).

Requires (provisioned by .amplifier/digital-twin-universe/profiles/memory-native-e2e.yaml):
  - amplifier-bundle-memory installed with behaviors/memory.yaml active
  - a Rust toolchain (amplifier-data's Rust kernel is built at install time)
  - the legacy vendor package absent
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

sys.path.insert(
    0, "/workspace/amplifier-bundle-memory/modules/tool-memory"
)  # pragma: no cover - DTU-only path
from amplifier_module_tool_memory.client import ensure_daemon  # noqa: E402


def test_native_memory_roundtrip_across_processes(memory_home: Path) -> None:
    """KG-N1: remember in this process, search-recall from a second process.

    Both processes discover the SAME auto-started daemon via
    ``daemon.json`` in the shared memory home -- there is no seeding step,
    no mock, and no shadow: this is the real daemon, the real embedder, and
    the real amplifier-data store.
    """
    wing = "wing_e2e"
    room = "e2e-smoke"
    content = "native memory e2e: verbatim decision content for the friend-scenario smoke test"

    client = ensure_daemon(memory_home)
    assert client is not None, "memory daemon unavailable in this process"
    ref = client.remember(
        wing=wing, room=room, content=content, source="test_native_memory_e2e"
    )
    assert ref

    # Give the daemon's embedding sweep (or synchronous embed) a moment.
    time.sleep(1.0)

    # ---- Second process: a fresh Python interpreter discovers the SAME daemon ----
    script = (
        "import sys; sys.path.insert(0, '/workspace/amplifier-bundle-memory/modules/tool-memory');"
        "from amplifier_module_tool_memory.client import ensure_daemon;"
        f"c = ensure_daemon('{memory_home}');"
        f"r = c.search('friend-scenario smoke test', 5, wing='{wing}', room='{room}');"
        "print(r)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"second-process search failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert content in result.stdout, (
        "second process did not recall the drawer filed by the first process "
        f"through the auto-started daemon.\noutput: {result.stdout}"
    )


def test_daemon_survives_kill_and_respawns(memory_home: Path) -> None:
    """KG-N6 (in vivo): kill -9 the daemon mid-run; the next operation respawns
    it and pre-crash drawers are still readable (the durable log is truth)."""
    client = ensure_daemon(memory_home)
    assert client is not None
    ref = client.remember(
        wing="wing_e2e", room="respawn-smoke", content="pre-crash drawer, must survive"
    )

    daemon_json = memory_home / "daemon.json"
    import json

    info = json.loads(daemon_json.read_text(encoding="utf-8"))
    pid = int(info["pid"])

    import os
    import signal

    os.kill(pid, signal.SIGKILL)
    time.sleep(0.5)

    new_client = ensure_daemon(memory_home)
    assert new_client is not None, "daemon did not respawn after kill -9"

    cell = new_client.regenerate(ref)
    assert cell.payload == b"pre-crash drawer, must survive", (
        "pre-crash drawer not readable after respawn -- durability broken"
    )
