"""T1-HOOK-1 — end-to-end behavioral write hook.

Acceptance: a synthetic FAILING session boosts the relevant drawer's importance
through a salience-gated, contract-complete, REVERSIBLE write. A neutral
all-success session writes nothing (default: don't learn).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from amplifier_module_hooks_behavioral_write import (
    BehavioralWriteHook,
    process_session,
)
from amplifier_module_tool_memory.phase3 import compute_importance
from amplifier_module_tool_memory.store import RecordingMemoryStore


def _events_file(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _drawer_filed(drawer_id: str, category: str, tool_success: bool) -> dict[str, Any]:
    return {
        "event": "drawer_filed",
        "data": {
            "drawer_id": drawer_id,
            "category": category,
            "tool_success": tool_success,
        },
    }


def test_failing_session_boosts_and_is_reversible(tmp_path: Path) -> None:
    events = _events_file(
        tmp_path, [_drawer_filed("drawer:d1", "decision", tool_success=False)]
    )
    store = RecordingMemoryStore()
    store.importance["drawer:d1"] = 0.1  # seed a low prior

    records = process_session(
        events,
        store,
        query_importance=lambda s: store.importance.get(s),
    )

    # Exactly one reversible write, and importance moved UP from the prior.
    assert len(records) == 1
    rec = records[0]
    expected_new = compute_importance("decision", {"unresolved": True})
    assert store.importance["drawer:d1"] == expected_new
    assert store.importance["drawer:d1"] > 0.1

    # Full contract present.
    assert rec.provenance == "hooks-behavioral-write:orchestrator:complete"
    assert rec.interaction_id and rec.rollback_handle == rec.interaction_id
    assert "tool_success=False" in rec.source_outcome
    assert rec.delta.old_value == "0.1"

    # Reversible: rollback restores the prior value exactly.
    store.rollback(rec)
    assert store.importance["drawer:d1"] == 0.1


def test_neutral_success_session_writes_nothing(tmp_path: Path) -> None:
    events = _events_file(
        tmp_path,
        [
            _drawer_filed("drawer:a", "general", tool_success=True),
            _drawer_filed("drawer:b", "pattern", tool_success=True),
        ],
    )
    store = RecordingMemoryStore()
    store.importance["drawer:a"] = 0.2
    store.importance["drawer:b"] = 0.2

    records = process_session(
        events, store, query_importance=lambda s: store.importance.get(s)
    )

    # Default: don't learn. No failures -> reward/surprise 0 -> gate rejects.
    assert records == []
    assert store.importance["drawer:a"] == 0.2
    assert store.mutations == []


def test_missing_events_file_is_safe_noop(tmp_path: Path) -> None:
    store = RecordingMemoryStore()
    records = process_session(
        tmp_path / "nope.jsonl", store, query_importance=lambda s: None
    )
    assert records == []


def test_hook_call_is_noop_without_store_factory() -> None:
    """Merely installing the hook never mutates memory on its own."""
    hook = BehavioralWriteHook({})
    result = asyncio.run(
        hook("orchestrator:complete", {"events_path": "/tmp/whatever.jsonl"})
    )
    assert result.action == "continue"
    assert hook.audit_log == []


def test_hook_dispatches_off_hot_path(tmp_path: Path) -> None:
    """__call__ returns immediately; the learning pass runs in a daemon thread
    and the audit log eventually records the reversible write."""
    import time

    events = _events_file(
        tmp_path, [_drawer_filed("drawer:d1", "decision", tool_success=False)]
    )
    store = RecordingMemoryStore()
    store.importance["drawer:d1"] = 0.1

    hook = BehavioralWriteHook(
        {},
        store_factory=lambda: store,
        query_importance=lambda s: store.importance.get(s),
    )
    result = asyncio.run(hook("orchestrator:complete", {"events_path": str(events)}))
    assert result.action == "continue"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not hook.audit_log:
        time.sleep(0.01)
    assert len(hook.audit_log) == 1
    assert store.importance["drawer:d1"] > 0.1
