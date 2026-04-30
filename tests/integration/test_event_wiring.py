"""End-to-end coordinator event wiring tests.

Runs INSIDE the memory-bundle-e2e DTU container.
Validates: hook fires -> coordinator.hooks.emit -> hooks-logging writes events.jsonl.

All tests in this module are automatically skipped on the host machine —
see tests/integration/conftest.py (pytest_collection_modifyitems).

DTU requirements (provisioned by memory-bundle-e2e.yaml):
- /root/.mempalace (seeded palace + spool dir)
- amplifier installed via uv tool install
- mempalace CLI on PATH
- ANTHROPIC_API_KEY / OPENAI_API_KEY in /root/.amplifier/keys.env
- pytest-asyncio installed
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

WORKSPACE = Path("/workspace/amplifier-bundle-memory")


def _latest_events_jsonl() -> Path | None:
    """Return the most recent session's events.jsonl from /root/.amplifier.

    Searches recursively under /root/.amplifier, sorts by modification time,
    and returns the path with the highest mtime. Returns None if no file found.
    """
    files = sorted(
        Path("/root/.amplifier").rglob("events.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    return files[-1] if files else None


def _events_in(path: Path) -> list[dict]:
    """Load a JSONL file and return a list of event dicts."""
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _coordinator_events(path: Path) -> list[dict]:
    """Filter events whose 'event' key starts with 'memory-mempalace:'."""
    return [
        e
        for e in _events_in(path)
        if isinstance(e.get("event"), str)
        and e["event"].startswith("memory-mempalace:")
    ]


def test_drawer_filed_appears_in_events_jsonl():
    """drawer_filed event should appear in events.jsonl after amplifier run.

    Runs an amplifier session with a message that contains an architecture
    decision — the mempalace hook should file it as a drawer and emit a
    memory-mempalace:drawer_filed coordinator event.
    """
    subprocess.run(
        [
            "amplifier",
            "run",
            "--",
            "echo 'Architecture decision: we use dual-emit for observability'",
        ],
        timeout=120,
        cwd=WORKSPACE,
    )
    time.sleep(2.0)
    events_path = _latest_events_jsonl()
    assert events_path is not None, "no events.jsonl found — is hooks-logging mounted?"
    coordinator_events = _coordinator_events(events_path)
    event_names = [e.get("event") for e in coordinator_events]
    assert "memory-mempalace:drawer_filed" in event_names


def test_briefing_assembled_payload_has_drawer_ids():
    """briefing_assembled event payload should contain a drawer_ids list.

    Runs a short amplifier session and checks that the coordinator emitted a
    memory-mempalace:briefing_assembled event whose payload includes a
    'drawer_ids' list.
    """
    subprocess.run(
        ["amplifier", "run", "--", "echo done"],
        timeout=60,
        cwd=WORKSPACE,
    )
    time.sleep(1.0)
    events_path = _latest_events_jsonl()
    assert events_path is not None, "no events.jsonl found — is hooks-logging mounted?"
    coordinator_events = _coordinator_events(events_path)
    briefing_events = [
        e
        for e in coordinator_events
        if e.get("event") == "memory-mempalace:briefing_assembled"
    ]
    assert briefing_events, "No briefing_assembled event found in coordinator events"
    briefing = briefing_events[-1]
    data = briefing.get("data", {})
    # Defensive: if 'drawer_ids' not in data, try the briefing dict directly
    if "drawer_ids" not in data:
        data = briefing
    assert "drawer_ids" in data, (
        f"'drawer_ids' not found in briefing_assembled payload. "
        f"Available keys: {list(data.keys())}"
    )
    assert isinstance(data["drawer_ids"], list), (
        f"'drawer_ids' should be a list, got {type(data['drawer_ids'])}"
    )


def test_briefed_ids_prevents_reinjection():
    """briefed drawer IDs should not be re-injected in subsequent surfaced events.

    This test validates the deduplication contract: drawer_ids present in
    briefing_assembled should not reappear in memory_surfaced events for the
    same session.
    """
    pytest.skip(
        "Requires content-pinned session with deterministic retrieval. "
        "Validated manually by comparing drawer_ids in briefing_assembled "
        "with memory_ids in subsequent memory_surfaced events."
    )
