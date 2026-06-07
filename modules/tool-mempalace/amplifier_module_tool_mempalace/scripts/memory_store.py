"""
MemoryStore — the storage seam for the consolidation pipeline.

The cold-path ``curate.dot`` pipeline produces consolidated "cells" and writes
them through a ``MemoryStore``. Today the only real backend is the MemPalace
drawer store (``PalaceMemoryStore``). The amplifier-data substrate is a declared
seam (``AmplifierDataMemoryStore``) that fails loudly until persistence + a
vector lens land in amplifier-data — it must never silently pretend to store.

Keeping this as a one-method protocol means the pipeline's ``write_cells`` node
is identical regardless of backend; only the injected store changes.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol, runtime_checkable

from amplifier_module_tool_mempalace.scripts.mutation import (
    MutationRecord,
    ReversibleDelta,
    new_mutation,
)


@runtime_checkable
class MemoryStore(Protocol):
    """A sink for consolidated memory cells."""

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        """Persist one consolidated cell."""
        ...


class RecordingMemoryStore:
    """In-memory store for tests and pipeline dry-runs.

    Records every cell instead of persisting it, so the pipeline data path can
    be exercised end-to-end with no MemPalace dependency.
    """

    def __init__(self) -> None:
        self.filed: list[dict[str, object]] = []
        # T1-MEM-2: mutation ledger + simulated current-importance map.
        self.mutations: list[MutationRecord] = []
        self.rolled_back: list[str] = []
        self.importance: dict[str, float] = {}

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        self.filed.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
            }
        )
        if importance is not None:
            self.importance[str(source or content)] = float(importance)

    def update_importance(
        self,
        subject: object,
        *,
        old_importance: float | None,
        new_importance: float,
        provenance: str,
        source_outcome: str,
        confidence: float,
        interaction_id: str | None = None,
    ) -> MutationRecord:
        """T1-MEM-2 (test seam): record an atomic has_importance UPDATE.

        The in-memory store is trivially atomic, so ``atomic=True`` here. It
        exists so the behavioral hook and contract can be exercised with no
        amplifier-data dependency.
        """
        delta = ReversibleDelta(
            subject=str(subject),
            predicate="has_importance",
            new_value=str(new_importance),
            old_value=None if old_importance is None else str(old_importance),
        )
        record = new_mutation(
            provenance=provenance,
            source_outcome=source_outcome,
            delta=delta,
            confidence=confidence,
            atomic=True,
            interaction_id=interaction_id,
        ).mark_applied()
        self.importance[str(subject)] = float(new_importance)
        self.mutations.append(record)
        return record

    def rollback(self, record: MutationRecord) -> None:
        """Reverse a prior UPDATE: restore old value, or drop if none existed."""
        d = record.delta
        if d.old_value is None:
            self.importance.pop(d.subject, None)
        else:
            self.importance[d.subject] = float(d.old_value)
        self.rolled_back.append(record.interaction_id)


class PalaceMemoryStore:
    """Writes consolidated cells as verbatim MemPalace drawers via the CLI.

    Mirrors the capture hook's ``_mcp_add_drawer`` call so consolidated content
    lands in the palace exactly like raw captures do.
    """

    def __init__(self, added_by: str = "curate-pipeline") -> None:
        self.added_by = added_by

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "tool": "mempalace_add_drawer",
                "arguments": {
                    "wing": wing,
                    "room": room,
                    "content": content,
                    "source_file": source,
                    "added_by": self.added_by,
                },
            }
        )
        subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True,
            text=True,
            timeout=15,
        )


class AmplifierDataMemoryStore:
    """Writes consolidated cells into the amplifier-data event-log substrate.

    Mapping onto amplifier-data's API (verified against the real library,
    docs/CONSUMER_INTEGRATION.md §3):
      - drawer content -> ``write_cell(bytes)`` (verbatim, content-addressed; E1)
      - wing/room      -> ``scope(ref, scope_cell)`` (``scoped_to`` edges;
                          hierarchy = multiple scope edges per drawer)
      - category/importance/source -> ``assert_fact(ref, predicate, object_cell)``
                          (queryable, invalidatable KG facts; the object of a
                          fact is itself a content-addressed cell)

    amplifier-data is an OPTIONAL dependency. Construction raises loudly if it
    is not installed — never a silent no-op.
    """

    def __init__(
        self,
        store: object | None = None,
        *,
        path: str | None = None,
        base_url: str | None = None,
        token: str | None = None,
        record_access: bool = False,
    ) -> None:
        if store is None:
            if base_url is not None and token is not None:
                # Authed MCP gateway: token-protected, single-writer, MCP-shaped.
                from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (
                    GatewayClient,
                )

                store = GatewayClient(base_url, token)
            elif base_url is not None:
                # Native companion server (localhost, no auth): funnel writes
                # through the single-writer server so multiple processes share
                # one palace safely.
                try:
                    from amplifier_data.client import RemoteStore
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError(
                        "AmplifierDataMemoryStore(base_url=...) requires amplifier-data."
                    ) from exc
                store = RemoteStore(base_url)
            else:
                try:
                    from amplifier_data import AmplifierStore
                except ImportError as exc:  # pragma: no cover - exercised when absent
                    raise RuntimeError(
                        "AmplifierDataMemoryStore requires the amplifier-data package, "
                        "which is not installed. Install it (pip install -e amplifier-data) "
                        "or use PalaceMemoryStore."
                    ) from exc
                # record_access=False by default: consolidation is a write path;
                # we do not want every later read to append AccessEvents (§5).
                store = AmplifierStore(path=path, record_access=record_access)
        self.store: Any = store
        self.filed: list[dict[str, object]] = []
        # T1-MEM-2: ledger of plasticity mutations applied through this seam.
        self.mutations: list[MutationRecord] = []
        self.rolled_back: list[str] = []

    def close(self) -> None:
        """Close the backing store if it supports it (RemoteStore does not)."""
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        s = self.store
        ref = s.write_cell(content.encode("utf-8"))  # type: ignore[attr-defined]
        # wing/room scoping — content-addressed scope cells (idempotent refs).
        s.scope(ref, s.write_cell(f"wing:{wing}".encode()))  # type: ignore[attr-defined]
        s.scope(ref, s.write_cell(f"room:{room}".encode()))  # type: ignore[attr-defined]
        # queryable KG facts; a fact's object must itself be a cell ref.
        if source:
            s.assert_fact(ref, "has_source", s.write_cell(source.encode()))  # type: ignore[attr-defined]
        if category is not None:
            s.assert_fact(  # type: ignore[attr-defined]
                ref, "has_category", s.write_cell(str(category).encode())
            )
        if importance is not None:
            s.assert_fact(  # type: ignore[attr-defined]
                ref, "has_importance", s.write_cell(str(importance).encode())
            )
        self.filed.append(
            {
                "ref": ref,
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
            }
        )
        return ref

    def _supports_atomic_update(self) -> bool:
        """True iff the substrate exposes a multi-event atomic primitive.

        amplifier-data does NOT expose one today (no ``update_fact``, no public
        ``append_batch``); this is the Step-3 / T3D-2 addition owned by the
        amplifier-data repo. We probe via getattr so this seam upgrades to the
        atomic path automatically the moment the primitive lands upstream.
        """
        s = self.store
        return callable(getattr(s, "update_fact", None)) or callable(
            getattr(s, "append_batch", None)
        )

    def update_importance(
        self,
        subject: Any,
        *,
        old_importance: float | None,
        new_importance: float,
        provenance: str,
        source_outcome: str,
        confidence: float,
        interaction_id: str | None = None,
    ) -> MutationRecord:
        """T1-MEM-2: replace a drawer's ``has_importance`` fact (UPDATE, not add).

        Atomic WHEN the substrate supports it; otherwise a sequential
        invalidate+assert that the MutationRecord (atomic=False) + rollback
        handle make recoverable. Carries the full mutation contract. Intended
        to run async / post-turn, never on the hot path.
        """
        s = self.store
        new_ref = s.write_cell(str(new_importance).encode())  # type: ignore[attr-defined]
        old_ref = (
            s.write_cell(str(old_importance).encode())  # type: ignore[attr-defined]
            if old_importance is not None
            else None
        )
        atomic = self._supports_atomic_update()
        delta = ReversibleDelta(
            subject=str(subject),
            predicate="has_importance",
            new_value=str(new_importance),
            old_value=None if old_importance is None else str(old_importance),
        )
        record = new_mutation(
            provenance=provenance,
            source_outcome=source_outcome,
            delta=delta,
            confidence=confidence,
            atomic=atomic,
            interaction_id=interaction_id,
        )
        update_fact = getattr(s, "update_fact", None)
        if atomic and callable(update_fact):
            # Future amplifier-data atomic primitive (T3D-2): one batch.
            update_fact(subject, "has_importance", old_ref, new_ref)
        else:
            # Degraded sequential path. NOT atomic: a crash between the two
            # leaves the fact invalidated-but-not-reasserted. Recoverable via
            # the rollback handle on the returned record.
            if old_ref is not None:
                s.invalidate_fact(subject, "has_importance", old_ref)  # type: ignore[attr-defined]
            s.assert_fact(subject, "has_importance", new_ref)  # type: ignore[attr-defined]
        record = record.mark_applied()
        self.mutations.append(record)
        return record

    def rollback(self, record: MutationRecord) -> None:
        """Reverse a prior UPDATE via the rollback handle: invalidate the new
        value and, if a prior value existed, re-assert it."""
        s = self.store
        d = record.delta
        new_ref = s.write_cell(str(d.new_value).encode())  # type: ignore[attr-defined]
        s.invalidate_fact(d.subject, d.predicate, new_ref)  # type: ignore[attr-defined]
        if d.old_value is not None:
            s.assert_fact(  # type: ignore[attr-defined]
                d.subject, d.predicate, s.write_cell(str(d.old_value).encode())
            )
        self.rolled_back.append(record.interaction_id)


class DualWriteMemoryStore:
    """Migration fan-out: write to a primary (source of truth) and a shadow.

    The primary stays authoritative (the palace today); the shadow
    (amplifier-data) is written in parallel so regenerated views can be compared
    against the primary. A shadow failure NEVER breaks the primary write — it is
    recorded in ``shadow_errors`` (set ``fail_on_shadow_error=True`` to surface
    it). This is the §8 dual-write migration pattern.
    """

    def __init__(
        self,
        primary: MemoryStore,
        shadow: MemoryStore,
        *,
        fail_on_shadow_error: bool = False,
    ) -> None:
        self.primary = primary
        self.shadow = shadow
        self.fail_on_shadow_error = fail_on_shadow_error
        self.shadow_errors: list[str] = []

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        self.primary.file(
            wing=wing,
            room=room,
            content=content,
            source=source,
            category=category,
            importance=importance,
        )
        try:
            self.shadow.file(
                wing=wing,
                room=room,
                content=content,
                source=source,
                category=category,
                importance=importance,
            )
        except Exception as exc:  # shadow must never break the source of truth
            self.shadow_errors.append(f"{type(exc).__name__}: {exc}")
            if self.fail_on_shadow_error:
                raise
