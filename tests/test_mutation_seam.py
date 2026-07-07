"""T1-MEM-2 + mutation contract: UPDATE seam and reversibility.

Verifies that the has_importance UPDATE path replaces (not appends), carries
the full mutation contract, and is reversible via the rollback handle. Uses
RecordingMemoryStore so no amplifier-data dependency is required.
"""

from __future__ import annotations

import pytest

from amplifier_module_tool_memory.store import RecordingMemoryStore
from amplifier_module_tool_memory.scripts.mutation import (
    ReversibleDelta,
    new_mutation,
)


def _update(store: RecordingMemoryStore, subject: str, old: float | None, new: float):
    return store.update_importance(
        subject,
        old_importance=old,
        new_importance=new,
        provenance="behavioral-write-hook:orchestrator:complete",
        source_outcome="tool_success=False",
        confidence=0.8,
    )


def test_update_carries_full_contract() -> None:
    store = RecordingMemoryStore()
    rec = _update(store, "drawer:d1", 0.5, 0.7)
    # Every field of the mutation contract must be present and meaningful.
    assert rec.provenance
    assert rec.interaction_id.startswith("ix_")
    assert rec.rollback_handle == rec.interaction_id
    assert rec.source_outcome == "tool_success=False"
    assert 0.0 <= rec.confidence <= 1.0
    assert rec.timestamp.endswith("+00:00")
    assert rec.delta == ReversibleDelta(
        subject="drawer:d1",
        predicate="has_importance",
        new_value="0.7",
        old_value="0.5",
    )
    assert rec.applied is True


def test_update_replaces_not_appends() -> None:
    store = RecordingMemoryStore()
    _update(store, "drawer:d1", 0.5, 0.7)
    _update(store, "drawer:d1", 0.7, 0.9)
    # Current value reflects the LATEST write, not an accumulation.
    assert store.importance["drawer:d1"] == 0.9
    assert len(store.mutations) == 2


def test_rollback_restores_prior_value() -> None:
    store = RecordingMemoryStore()
    rec = _update(store, "drawer:d1", 0.5, 0.7)
    assert store.importance["drawer:d1"] == 0.7
    store.rollback(rec)
    assert store.importance["drawer:d1"] == 0.5
    assert rec.interaction_id in store.rolled_back


def test_rollback_drops_value_that_had_no_prior() -> None:
    store = RecordingMemoryStore()
    rec = _update(store, "drawer:new", None, 0.6)
    assert store.importance["drawer:new"] == 0.6
    store.rollback(rec)
    assert "drawer:new" not in store.importance


def test_contract_rejects_empty_provenance() -> None:
    with pytest.raises(ValueError):
        new_mutation(
            provenance="",
            source_outcome="x",
            delta=ReversibleDelta("s", "has_importance", "0.1"),
            confidence=0.5,
            atomic=True,
        )


def test_contract_rejects_empty_source_outcome() -> None:
    with pytest.raises(ValueError):
        new_mutation(
            provenance="hook",
            source_outcome="",
            delta=ReversibleDelta("s", "has_importance", "0.1"),
            confidence=0.5,
            atomic=True,
        )


def test_recording_seam_marks_atomic_true() -> None:
    """In-memory seam is trivially atomic; real amplifier-data path is not yet."""
    store = RecordingMemoryStore()
    rec = _update(store, "drawer:d1", 0.5, 0.7)
    assert rec.atomic is True
