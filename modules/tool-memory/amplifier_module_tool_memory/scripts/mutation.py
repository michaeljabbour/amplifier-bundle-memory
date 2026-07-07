"""The plasticity mutation contract (shared by T1-MEM-2 and T1-HOOK-1).

EVERY plasticity write in this constellation must carry the full contract:

    provenance            - what produced the write (hook + event)
    causal interaction id - the causal tag linking write -> originating outcome
    reversible delta      - the exact before/after so the write can be undone
    timestamp             - ISO-8601 UTC, when the write was minted
    source outcome        - the observed outcome that justified the write
    confidence            - [0,1], how sure we are this write is correct
    rollback handle       - an opaque id used to locate + reverse the write

This module is pure data: no I/O, no store reference, fully serialisable so
the record can be appended to an audit log. The *executable* rollback lives in
the seam (which holds the amplifier-data store); here we carry only the inverse
delta needed to perform it.

Note on the causal interaction id: amplifier-data does not yet expose a native
``write_interaction_id`` / causal-tag primitive (that is a Step-3 addition owned
by the amplifier-data repo). Until it lands we mint the id here at the seam so
the contract is honoured end-to-end; when the substrate exposes the primitive
this id maps directly onto it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

__all__ = [
    "ReversibleDelta",
    "MutationRecord",
    "new_interaction_id",
    "new_mutation",
]


def new_interaction_id() -> str:
    """Mint a fresh causal interaction id (seam-level until T3D lands)."""
    return f"ix_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ReversibleDelta:
    """A single before/after change to one KG fact.

    ``old_value is None`` means the fact did not exist before the write, so the
    inverse is "invalidate the new value" rather than "restore the old".
    """

    subject: str
    predicate: str
    new_value: str
    old_value: str | None = None

    def inverse(self) -> "ReversibleDelta":
        """The delta that undoes this one.

        If there was no prior value, the inverse re-targets the new value back
        to itself with ``old_value`` recording the value to invalidate; callers
        (the seam) interpret ``old_value is None`` on the inverse as a pure
        invalidate-new with no re-assert.
        """
        return ReversibleDelta(
            subject=self.subject,
            predicate=self.predicate,
            new_value=self.old_value if self.old_value is not None else self.new_value,
            old_value=None if self.old_value is not None else self.new_value,
        )


@dataclass(frozen=True)
class MutationRecord:
    """The full, serialisable record of one plasticity write."""

    interaction_id: str
    provenance: str
    source_outcome: str
    delta: ReversibleDelta
    confidence: float
    timestamp: str
    rollback_handle: str
    atomic: bool
    applied: bool = False
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    def mark_applied(self) -> "MutationRecord":
        """Return a copy with ``applied=True`` (records are frozen)."""
        data = asdict(self)
        data["delta"] = self.delta  # keep dataclass, not dict
        data["applied"] = True
        return MutationRecord(**data)


def new_mutation(
    *,
    provenance: str,
    source_outcome: str,
    delta: ReversibleDelta,
    confidence: float,
    atomic: bool,
    interaction_id: str | None = None,
) -> MutationRecord:
    """Construct a contract-complete MutationRecord with a fresh timestamp+id.

    Validates the contract: confidence is clamped to [0,1]; provenance and
    source_outcome must be non-empty (a write with no provenance or no source
    outcome is a contract violation and is rejected loudly).
    """
    if not provenance:
        raise ValueError("mutation contract violation: empty provenance")
    if not source_outcome:
        raise ValueError("mutation contract violation: empty source_outcome")
    iid = interaction_id or new_interaction_id()
    conf = max(0.0, min(1.0, float(confidence)))
    return MutationRecord(
        interaction_id=iid,
        provenance=provenance,
        source_outcome=source_outcome,
        delta=delta,
        confidence=conf,
        timestamp=datetime.now(UTC).isoformat(),
        rollback_handle=iid,
        atomic=atomic,
    )
