"""End-to-end coordinator event wiring tests.

Runs INSIDE the memory-native-e2e DTU container.
Validates: hook fires -> coordinator.hooks.emit -> hooks-logging writes events.jsonl.

All tests in this module are automatically skipped on the host machine --
see tests/integration/conftest.py (pytest_collection_modifyitems).

DTU requirements (provisioned by memory-native-e2e.yaml):
- amplifier installed via uv tool install
- the memory bundle registered via behaviors/memory.yaml
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
    """Filter events whose 'event' key starts with the native `memory:` prefix
    (native cutover: was the vendor-branded coordinator-bridge prefix)."""
    return [
        e
        for e in _events_in(path)
        if isinstance(e.get("event"), str) and e["event"].startswith("memory:")
    ]


def _poll_for_event_name(
    event_name: str,
    *,
    timeout: float = 30.0,
    interval: float = 1.0,
) -> tuple[bool, list[dict]]:
    """Poll the freshest events.jsonl until ``event_name`` appears or timeout.

    The drain thread that files drawers and bridges ``memory:drawer_filed``
    into events.jsonl runs off the hot path (see hooks-memory-capture's
    module docstring), so there is an inherent -- if normally small -- delay
    between the tool call completing and the event landing in the log. A
    single fixed sleep is not generous enough to be robust against that
    delay under DTU load; poll instead.

    Returns ``(found, last_seen_coordinator_events)`` so callers can build an
    informative assertion message from whatever WAS observed even on
    timeout.
    """
    deadline = time.monotonic() + timeout
    last_seen: list[dict] = []
    while True:
        events_path = _latest_events_jsonl()
        if events_path is not None:
            last_seen = _coordinator_events(events_path)
            if any(e.get("event") == event_name for e in last_seen):
                return True, last_seen
        if time.monotonic() >= deadline:
            return False, last_seen
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Deterministic capture-worthy payload
# ---------------------------------------------------------------------------
#
# hooks-memory-capture (modules/hooks-memory-capture/.../__init__.py) gates
# every tool:post output through two checks before it becomes a
# `memory:drawer_filed` event:
#
#   1. _is_memory_worthy(): 50 < len(output) <= 8192 bytes, and the tool
#      isn't in the skip_tools set ({"memory_status", "memory_reconnect",
#      "memory_hook_settings"}).
#   2. category filter: behaviors/memory.yaml configures
#      `categories: [decision]` for this hook, so the detected category
#      must be "decision". Category detection (_detect_category /
#      manifest.detect_category) walks the manifest's attractors IN
#      DECLARATION ORDER and returns the FIRST one with a matching seed
#      (context/memory-manifest.yaml: "decision" is declared first, ahead
#      of "architecture"). So text containing a decision-seed word
#      ("decided", "decision", "we will", "going with", "chosen", "agreed")
#      resolves to category "decision" even if it ALSO contains an
#      architecture-seed word ("architecture", "design", "pattern", ...).
#
# The text below is built with a large safety margin against both gates
# (well over 50 bytes, nowhere near 8192) and contains "decision" as an
# unambiguous decision-category seed.
_DECISION_TEXT = (
    "Architecture decision: we have decided to use dual-emit for "
    "observability. This decision routes every memory-capture completion "
    "through both the session event log and the coordinator bridge, so "
    "drawer_filed events reach events.jsonl in real time for downstream "
    "consolidation, auditing, and the memory briefing hook in future "
    "sessions. Recording this decision verbatim for the event-wiring "
    "regression test."
)
assert 50 < len(_DECISION_TEXT) < 8192  # guards the fixture itself, not the DTU run

_CAPTURE_COMMAND = f"echo '{_DECISION_TEXT}'"

# The prompt is deliberately an explicit, unambiguous IMPERATIVE to invoke a
# tool with a literal command -- not a free-form statement that merely
# *resembles* a shell command. The original test's prompt (`amplifier run --
# "echo '...'"`) handed the model a topic to discuss; whether the model
# elected to run anything, and what it chose to run if it did, was entirely
# the model's call. Diagnosed in a real DTU failure: the session made
# exactly one tool call whose output was too short (capture_skipped,
# reason=too_short) -- the model did not execute the embedded text
# verbatim, it improvised a much shorter command of its own.
#
# This prompt removes that degree of freedom: it tells the model there is
# exactly one required action (call the tool, run this exact command) and
# nothing else is being asked of it. Coding-agent models are highly
# reliable at following a single, explicit, literal instruction like this
# -- this is the same operating mode every other tool call in a session
# depends on. Combined with the conftest workspace-hygiene fixture (which
# stops a prior session's project-context edits from making the model think
# the "decision" is already handled and this step can be skipped), the
# residual non-determinism is materially different in kind from the
# original bug: before, *whether and what* to run was open-ended; now only
# faithful execution of a single named instruction is required, which the
# shell -- not the model -- ultimately produces the output for.
_CAPTURE_PROMPT = (
    "AUTOMATED TEST INSTRUCTION (not a real user request). You have exactly "
    "one required action: call your bash/shell tool right now and run the "
    "following command verbatim. Do not shorten it, do not paraphrase it, "
    "do not summarize it, and do not run any other command before or "
    "instead of it. After it completes, reply with one short line "
    "confirming it ran.\n\n"
    f"Command:\n{_CAPTURE_COMMAND}"
)


def test_drawer_filed_appears_in_events_jsonl():
    """drawer_filed event should appear in events.jsonl after amplifier run.

    Drives an explicit, deterministic tool call (see _CAPTURE_PROMPT above)
    through a real `amplifier run` session so the capture hook's real
    tool:post -> _process_job -> _DRAIN_BRIDGE -> coordinator.hooks.emit ->
    events.jsonl path is exercised end-to-end, exactly as a live user
    session would exercise it. This intentionally does NOT collapse to a
    unit test of the hook in isolation -- that path is already covered by
    modules/hooks-memory-capture/tests/; this test exists to prove the
    live-session bridge.
    """
    result = subprocess.run(
        ["amplifier", "run", "--", _CAPTURE_PROMPT],
        timeout=120,
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=False,
    )

    found, coordinator_events = _poll_for_event_name(
        "memory:drawer_filed", timeout=30.0, interval=1.0
    )

    event_names = [e.get("event") for e in coordinator_events]
    skipped = [
        e.get("data", {}).get("reason")
        for e in coordinator_events
        if e.get("event") == "memory:capture_skipped"
    ]
    assert found, (
        "memory:drawer_filed did not appear in events.jsonl within the poll "
        "window.\n"
        f"coordinator events observed: {event_names}\n"
        f"capture_skipped reasons observed: {skipped}\n"
        f"amplifier run exit code: {result.returncode}\n"
        f"amplifier run stdout: {result.stdout[-2000:]}\n"
        f"amplifier run stderr: {result.stderr[-2000:]}"
    )


def test_briefing_assembled_payload_has_drawer_ids():
    """briefing_assembled event payload should contain a drawer_ids list.

    Runs a short amplifier session and checks that the coordinator emitted a
    memory:briefing_assembled event whose payload includes a 'drawer_ids' list.
    """
    subprocess.run(
        ["amplifier", "run", "--", "echo done"],
        timeout=60,
        cwd=WORKSPACE,
    )
    time.sleep(1.0)
    events_path = _latest_events_jsonl()
    assert events_path is not None, "no events.jsonl found -- is hooks-logging mounted?"
    coordinator_events = _coordinator_events(events_path)
    briefing_events = [
        e for e in coordinator_events if e.get("event") == "memory:briefing_assembled"
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
