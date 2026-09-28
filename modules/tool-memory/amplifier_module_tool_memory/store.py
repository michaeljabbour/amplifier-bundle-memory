"""
MemoryStore — the storage seam for the consolidation pipeline.

The cold-path ``curate.dot`` pipeline produces consolidated "cells" and writes
them through a ``MemoryStore``. ``NativeMemoryStore`` is the ONE store
now (native cutover, B2, docs/plans/2026-07-07-native-cutover-design.md) --
drawers, scopes, KG facts, vectors, and diary entries all route through it,
with three interchangeable backends (direct ``AmplifierStore``, ``RemoteStore``
via the companion server, and the authed ``GatewayClient``/``MemoryClient``).

Keeping ``file()`` as the common protocol method means the pipeline's
``write_cells`` node is identical regardless of backend; only the injected
store changes. The additional seam surfaces (``search_vectors``, ``assert_kg``
/ ``query_kg`` / ``kg_timeline``, ``file_diary``, and the §3.2 native read
surfaces: ``search``, ``list_drawers``, ``read_diary``, ``status``,
``kg_stats``) do not change the core ``file()`` contract.

DELETED in B2 (no longer needed once every write path is native): the
legacy vendor JSON-RPC-over-stdio transport (``_call_mcp_tool``,
``_MCP_PROTOCOL_VERSION``), the legacy vendor-backed store (wrote verbatim
drawers via that transport), and ``DualWriteMemoryStore`` (fanned out to a
primary + shadow -- there is no shadow anymore, the daemon IS the store).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from amplifier_module_tool_memory.scripts.mutation import (
    MutationRecord,
    ReversibleDelta,
    new_mutation,
)

_logger = logging.getLogger(__name__)

#: T1.2 (D9) capability check, not a hard dependency: BM25Index lives
#: upstream in amplifier-data (lenses/bm25.py). When it is not importable
#: (an older amplifier-data pin), RRF fusion is simply unavailable and
#: `NativeMemoryStore.search` always takes the fusion="legacy" path --
#: never a hard failure.
try:
    from amplifier_data.lenses.bm25 import BM25Index
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    BM25Index = None  # type: ignore[assignment,misc]
    _logger.debug(
        "amplifier_data.lenses.bm25.BM25Index is not available "
        "(amplifier-data pin predates the bm25 lens); "
        "NativeMemoryStore.search falls back to fusion='legacy'."
    )

#: Fact predicates carrying time/provenance on a drawer (D8: consumer-supplied
#: facts, never kernel event fields -- amplifier-data deliberately excludes
#: wall-clock time from events to keep byte-identical regeneration).
_FILED_AT_PREDICATE = "filed_at"
_IN_SESSION_PREDICATE = "in_session"
_AT_COMMIT_PREDICATE = "at_commit"

#: RRF constant (T1.2, D9): rrf = \u03a3_arms 1 / (_RRF_K + rank), rank 1-based.
_RRF_K = 60

#: T1.4 hot-path latency gate (design \u00a76 measurement bar): search p95 on a
#: 5k-drawer synthetic store must stay under this budget. A module constant
#: (not a magic number in the test) so the budget is visible and tunable in
#: one place.
SEARCH_P95_BUDGET_MS = 300


def _resolve_batch_ref(commit_result: Any, ref: Any) -> Any:
    """Resolve a batch-staged ref after ``commit()``.

    Direct ``WriteBatch.commit()`` (amplifier_data.envelope) returns
    ``list[SeqPos]``; refs staged via ``write_cell`` are already the real
    content-addressed hash, computed locally. ``GatewayWriteBatch.commit()``
    (amplifier_data_gateway) returns ``{pending_token: real_ref}`` because the
    client cannot replicate the substrate's addressing algorithm client-side
    -- resolve through the map when commit() returns one.
    """
    if isinstance(commit_result, dict):
        return commit_result.get(ref, ref)
    return ref


class _SearchFold:
    """ONE materialized ``kernel.all_events()`` pass, shared across every lens
    read inside a single :meth:`NativeMemoryStore.search` call.

    Perf seam (perf/search-no-regenerate): the old search path called
    ``store.regenerate(ref)`` per candidate/hit, and each store-surface lens
    read (``query_vector``/``query_facts``/``graph_neighbors``) re-materialized
    the whole event log — O(reads × log) per query (~12s on a ~38k-event
    durable store). This class pays for the materialization ONCE and exposes
    the only kernel surface the substrate's pure-fold lenses consume
    (``all_events()``), so ``VectorLens``/``TemporalLens``/``GraphLens``/
    ``fold_scope`` run their EXACT own logic over one cached event list —
    reuse of amplifier-data primitives, not a reimplementation.

    It also carries the ``ref -> payload`` join for every ``CellWriteEvent``
    seen in that pass (the same join ``VectorLens.project`` performs), so
    per-hit payload reads need no ``regenerate`` full-log re-fold. Content
    addressing makes the join exact: a ref defined by a ``CellWriteEvent`` is
    the content address of ``(payload, interpreters)``, so every defining
    event for that ref carries an identical payload — precisely what
    ``regenerate(ref).payload`` returns (replay.py folds to the last defining
    payload). Refs defined some other way (interpreter/index cells) are
    absent from the join and fall back to ``regenerate``.

    Reads the kernel directly — no AccessEvents (D4 read-vs-fold boundary).
    Scoped to one call: never cached on the store (D1: lenses stay pure;
    every search folds a fresh, current view of the log).
    """

    def __init__(self, kernel: Any) -> None:
        from amplifier_data.models import CellWriteEvent, RelationshipEvent

        self._events: Any = kernel.all_events()
        payloads: dict[Any, bytes] = {}
        for _pos, ev in self._events:
            if isinstance(ev, CellWriteEvent):
                payloads[ev.cell_ref()] = ev.payload
        self.payloads: dict[Any, bytes] = payloads

        # T0.2: earliest `filed_at` per subject ref, in a second O(log) pass
        # over the SAME materialized event list (still no per-hit re-fold).
        # `filed_at` is never invalidated (append-only provenance); a ref
        # re-filed under a different `filed_at` carries multiple assertions,
        # so "first-seen" is the lexicographically (== chronologically,
        # ISO-8601) smallest value. See NativeMemoryStore.drawer_times.
        filed_at: dict[Any, str] = {}
        for _pos, ev in self._events:
            if isinstance(ev, RelationshipEvent) and ev.type == _FILED_AT_PREDICATE:
                raw = payloads.get(ev.to_ref)
                if raw is None:
                    continue  # defensive: object cell not seen in this fold
                value = raw.decode("utf-8", errors="replace")
                existing = filed_at.get(ev.from_ref)
                if existing is None or value < existing:
                    filed_at[ev.from_ref] = value
        self.filed_at: dict[Any, str] = filed_at

    def all_events(self) -> Any:
        """The read accessor the fold lenses use (mirrors ``StorageKernel``)."""
        return self._events


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
        embedding: Sequence[float] | None = None,
        filed_at: str | None = None,
        session_id: str | None = None,
        commit: str | None = None,
    ) -> None:
        """Persist one consolidated cell.

        ``embedding``, when provided, is a pre-computed vector to transport
        alongside the cell. The seam NEVER computes embeddings itself (the
        embedder is bundle policy, per COMPOSITION.md) — it only carries a
        vector a caller already has.

        ``filed_at``/``session_id``/``commit`` are time/provenance facts
        (D8): ``filed_at`` defaults to now (UTC) when omitted; ``session_id``
        and ``commit`` are omitted entirely when ``None``.
        """
        ...


class RecordingMemoryStore:
    """In-memory store for tests and pipeline dry-runs.

    Records every cell instead of persisting it, so the pipeline data path can
    be exercised end-to-end with no vendor dependency.
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
        embedding: Sequence[float] | None = None,
        filed_at: str | None = None,
        session_id: str | None = None,
        commit: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "wing": wing,
            "room": room,
            "content": content,
            "filed_at": filed_at
            if filed_at is not None
            else datetime.now(UTC).isoformat(timespec="seconds"),
            "session_id": session_id,
            "commit": commit,
            "source": source,
            "category": category,
            "importance": importance,
        }
        if embedding is not None:
            record["embedding"] = list(embedding)
        self.filed.append(record)
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


class NativeMemoryStore:
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
                from amplifier_module_tool_memory.daemon import (
                    GatewayClient,
                )

                store = GatewayClient(base_url, token)
            elif base_url is not None:
                # Native companion server (localhost, no auth): funnel writes
                # through the single-writer server so multiple processes share
                # one store safely.
                try:
                    from amplifier_data.client import RemoteStore
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError(
                        "NativeMemoryStore(base_url=...) requires amplifier-data."
                    ) from exc
                store = RemoteStore(base_url)
            else:
                try:
                    from amplifier_data import AmplifierStore
                except ImportError as exc:  # pragma: no cover - exercised when absent
                    raise RuntimeError(
                        "NativeMemoryStore requires the amplifier-data package, "
                        "which is not installed. Install it (pip install -e amplifier-data)."
                    ) from exc
                # record_access=False by default: consolidation is a write path;
                # we do not want every later read to append AccessEvents (§5).
                store = AmplifierStore(path=path, record_access=record_access)
        self.store: Any = store
        self.filed: list[dict[str, object]] = []
        # T1-MEM-2: ledger of plasticity mutations applied through this seam.
        self.mutations: list[MutationRecord] = []
        self.rolled_back: list[str] = []
        # T1.2 (D9): ONE persistent BM25Index per store instance. Safe
        # because the daemon is single-writer over an append-only log --
        # incremental `.add()` (idempotent per ref, see BM25Index docstring)
        # is correct without ever needing to rebuild from scratch. `None`
        # when amplifier-data predates the bm25 lens (see the module-level
        # capability check above).
        self._bm25_index: Any = BM25Index() if BM25Index is not None else None

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
        embedding: Sequence[float] | None = None,
        filed_at: str | None = None,
        session_id: str | None = None,
        commit: str | None = None,
    ) -> Any:
        """Persist one drawer; returns the content-addressed cell ref.

        Pre-existing annotation bug fixed in the cold-start data-loss pass:
        this override always returns `ref` (every caller -- daemon.py's
        `remember` dispatch, the KG-N7 sweep tests -- relies on it), but was
        annotated `-> None` (copied from the `MemoryStore` Protocol, which
        genuinely is a fire-and-forget sink). `Any` because `ref`'s concrete
        type varies by backend (str for GatewayClient/RemoteStore, a `Hash`
        for the direct AmplifierStore path) -- the same reason `Any` is used
        for `self.store` itself on this class.

        Time/provenance facts (D8, T0.2): ``filed_at``/``in_session``/
        ``at_commit`` are asserted on the drawer ref in the SAME atomic batch
        as the existing has_source/has_category/has_importance facts.
        ``filed_at`` defaults to now (UTC, ISO-8601, second precision) when
        omitted -- it is ALWAYS asserted. ``in_session``/``at_commit`` are
        omitted entirely when ``session_id``/``commit`` is ``None``.
        Content-addressing caveat: identical content re-filed yields the
        SAME ref, so re-filing asserts another `filed_at` fact rather than
        replacing one (append-only) -- readers use the EARLIEST `filed_at`
        as first-seen (see :meth:`drawer_times`).
        """
        s = self.store
        if filed_at is None:
            filed_at = datetime.now(UTC).isoformat(timespec="seconds")
        if self._supports_atomic_update():
            # Batch path: cell + 2 scope edges + facts + optional embedding,
            # staged on ONE WriteBatch and committed as ONE atomic append_batch.
            # Refs are knowable pre-commit (content addressing), so the staged
            # graph wires exactly as the sequential path below does.
            b = s.write_batch()  # type: ignore[attr-defined]
            ref = b.write_cell(content.encode("utf-8"))
            b.scope(ref, b.write_cell(f"wing:{wing}".encode()))
            b.scope(ref, b.write_cell(f"room:{room}".encode()))
            if source:
                b.assert_fact(ref, "has_source", b.write_cell(source.encode()))
            if category is not None:
                b.assert_fact(ref, "has_category", b.write_cell(str(category).encode()))
            if importance is not None:
                b.assert_fact(
                    ref, "has_importance", b.write_cell(str(importance).encode())
                )
            b.assert_fact(ref, _FILED_AT_PREDICATE, b.write_cell(filed_at.encode()))
            if session_id is not None:
                b.assert_fact(
                    ref, _IN_SESSION_PREDICATE, b.write_cell(session_id.encode())
                )
            if commit is not None:
                b.assert_fact(ref, _AT_COMMIT_PREDICATE, b.write_cell(commit.encode()))
            if embedding is not None:
                # Byte-identical to add_embedding's own packing (store.py):
                # LE-f32, so E1/regeneration equivalence holds across paths.
                from amplifier_data.lenses.vector import EMBEDDING_OF

                vec = list(embedding)
                emb_ref = b.write_cell(struct.pack(f"<{len(vec)}f", *vec))
                b.relate(emb_ref, ref, EMBEDDING_OF)
            ref = _resolve_batch_ref(b.commit(), ref)
        else:
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
            s.assert_fact(  # type: ignore[attr-defined]
                ref, _FILED_AT_PREDICATE, s.write_cell(filed_at.encode())
            )
            if session_id is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    ref, _IN_SESSION_PREDICATE, s.write_cell(session_id.encode())
                )
            if commit is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    ref, _AT_COMMIT_PREDICATE, s.write_cell(commit.encode())
                )
            if embedding is not None:
                # Sequential path: the substrate's own add_embedding (dim-agnostic).
                s.add_embedding(ref, list(embedding))  # type: ignore[attr-defined]
        self.filed.append(
            {
                "ref": ref,
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
                "embedding": list(embedding) if embedding is not None else None,
                "filed_at": filed_at,
                "session_id": session_id,
                "commit": commit,
            }
        )
        return ref

    def search_vectors(
        self, vector: Sequence[float], k: int, *, wing: str | None = None
    ) -> list[tuple[Any, float]]:
        """Top-k cosine over shadowed embeddings, optionally scoped to a wing.

        Verify-only read surface (§4a of the substrate-adapter-completion
        design): scope ref is recomputed via ``write_cell(f"wing:{wing}")``
        — content addressing makes this idempotent (no duplicate cell, same
        ref) so no wing needs to have been filed through this call to query it.
        """
        s = self.store
        scope = s.write_cell(f"wing:{wing}".encode()) if wing else None  # type: ignore[attr-defined]
        return s.query_vector(list(vector), k, scope=scope)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # KG facts via anchor cells (§4b)
    # ------------------------------------------------------------------

    def _anchor(self, name: str) -> Any:
        """Content-addressed anchor cell for a string KG entity (``entity:{name}``).

        KG entities are strings; substrate facts are ``(Hash, str, Hash)``.
        Content addressing makes this mapping deterministic, idempotent, and
        collision-free against the existing ``wing:``/``room:`` scope cells.
        """
        return self.store.write_cell(f"entity:{name}".encode())  # type: ignore[attr-defined]

    def assert_kg(self, subject: str, predicate: str, object: str) -> None:
        """String-keyed KG assert: strings in, anchor-cell fact in the substrate."""
        s = self.store
        s.assert_fact(self._anchor(subject), predicate, self._anchor(object))  # type: ignore[attr-defined]

    def invalidate_kg(self, subject: str, predicate: str, object: str) -> None:
        s = self.store
        s.invalidate_fact(self._anchor(subject), predicate, self._anchor(object))  # type: ignore[attr-defined]

    def query_kg(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Currently-valid facts; anchor refs resolved back to entity strings
        via ``regenerate(record_access=False)``. Verify-only read surface."""
        s = self.store
        subj_ref = self._anchor(subject) if subject is not None else None
        res = s.query_facts(subject=subj_ref, predicate=predicate)  # type: ignore[attr-defined]
        out: list[tuple[str, str, str]] = []
        for fact in res.output:
            subj_name = self._resolve_anchor(fact.subject)
            obj_name = self._resolve_anchor(fact.object)
            out.append((subj_name, fact.predicate, obj_name))
        return out

    def _resolve_anchor(self, ref: Any) -> str:
        """Resolve an anchor cell ref back to its ``entity:{name}`` string."""
        s = self.store
        payload = s.regenerate(ref, record_access=False).payload.decode("utf-8")  # type: ignore[attr-defined]
        prefix = "entity:"
        return payload.removeprefix(prefix)

    def kg_timeline(self, subject: str) -> list[dict[str, Any]]:
        """SeqPos-ordered assert/invalidate history for one entity (wraps
        ``store.timeline(self._anchor(subject))``)."""
        s = self.store
        entries = s.timeline(self._anchor(subject))  # type: ignore[attr-defined]
        return [
            {
                "seq_pos": e.seq_pos,
                "op": e.op,
                "predicate": e.predicate,
                "object": self._resolve_anchor(e.object),
            }
            for e in entries
        ]

    # ------------------------------------------------------------------
    # Diary entries → cells (§4c)
    # ------------------------------------------------------------------

    def file_diary(self, *, agent_name: str, entry: str, topic: str = "general") -> Any:
        """Diary entry as a cell, scoped to the agent and the topic.

        Scope cells: ``agent:{agent_name}`` (per-agent scope — a scope axis
        orthogonal to wings) and ``room:{topic}`` (reuses the existing room
        convention). A ``has_source`` fact marks provenance (``diary:{agent_name}``).
        Atomic batch when supported (§4d), sequential fallback otherwise.
        """
        s = self.store
        if self._supports_atomic_update():
            b = s.write_batch()  # type: ignore[attr-defined]
            ref = b.write_cell(entry.encode("utf-8"))
            b.scope(ref, b.write_cell(f"agent:{agent_name}".encode()))
            b.scope(ref, b.write_cell(f"room:{topic}".encode()))
            b.assert_fact(
                ref, "has_source", b.write_cell(f"diary:{agent_name}".encode())
            )
            ref = _resolve_batch_ref(b.commit(), ref)
        else:
            ref = s.write_cell(entry.encode("utf-8"))  # type: ignore[attr-defined]
            s.scope(ref, s.write_cell(f"agent:{agent_name}".encode()))  # type: ignore[attr-defined]
            s.scope(ref, s.write_cell(f"room:{topic}".encode()))  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                ref, "has_source", s.write_cell(f"diary:{agent_name}".encode())
            )
        return ref

    def _supports_atomic_update(self) -> bool:
        """True iff the backend exposes the WriteBatch atomic primitive.

        Direct AmplifierStore: yes (envelope.WriteBatch, shipped at c1107b4).
        GatewayClient: yes (the gateway 'batch' tool).
        RemoteStore: NO — the companion server has no batch endpoint; the seam
        degrades to the sequential path and records atomic=False honestly.
        """
        s = self.store
        return callable(getattr(s, "write_batch", None))

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

        Atomic WHEN the substrate supports it (``write_batch``, shipped in
        amplifier-data's ``envelope`` module): the invalidate-old + assert-new
        pair lands in ONE ``kernel.append_batch`` call, all-or-nothing.
        Otherwise a sequential invalidate+assert that the MutationRecord
        (atomic=False) + rollback handle make recoverable. Carries the full
        mutation contract. Intended to run async / post-turn, never on the
        hot path.
        """
        s = self.store
        atomic = self._supports_atomic_update()
        if atomic:
            # ONE atomic batch: stage the invalidate-old relate (WriteBatch has
            # no invalidate_fact sugar -- the __invalidate__:-prefixed relate IS
            # the documented reserved-type convention) + assert-new, commit once.
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            new_ref = b.write_cell(str(new_importance).encode())
            old_ref = (
                b.write_cell(str(old_importance).encode())
                if old_importance is not None
                else None
            )
            if old_ref is not None:
                b.relate(subject, old_ref, INVALIDATE_PREFIX + "has_importance")
            b.assert_fact(subject, "has_importance", new_ref)
            b.commit()
        else:
            new_ref = s.write_cell(str(new_importance).encode())  # type: ignore[attr-defined]
            old_ref = (
                s.write_cell(str(old_importance).encode())  # type: ignore[attr-defined]
                if old_importance is not None
                else None
            )
            # Degraded sequential path. NOT atomic: a crash between the two
            # leaves the fact invalidated-but-not-reasserted. Recoverable via
            # the rollback handle on the returned record.
            if old_ref is not None:
                s.invalidate_fact(subject, "has_importance", old_ref)  # type: ignore[attr-defined]
            s.assert_fact(subject, "has_importance", new_ref)  # type: ignore[attr-defined]
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

    # ------------------------------------------------------------------
    # Native read surfaces (§3.2 of docs/plans/2026-07-07-native-cutover-design.md)
    # ------------------------------------------------------------------

    def _scope_ref(self, kind: str, name: str) -> Any:
        """Content-addressed scope cell ref for ``{kind}:{name}`` (e.g. ``wing:w``).

        Idempotent via content addressing -- no wing/room/agent needs to have
        been filed through this call to compute its ref.
        """
        return self.store.write_cell(f"{kind}:{name}".encode())  # type: ignore[attr-defined]

    def _fold_snapshot(self) -> _SearchFold | None:
        """One :class:`_SearchFold` over the backing kernel, or ``None``.

        ``None`` when the backend exposes no foldable kernel (RemoteStore /
        GatewayClient), signalling callers to keep the per-ref store-surface
        path (``regenerate`` / ``query_facts`` / ``graph_neighbors``)
        unchanged.
        """
        kernel = getattr(self.store, "kernel", None)
        if kernel is None or not callable(getattr(kernel, "all_events", None)):
            return None
        return _SearchFold(kernel)

    def _payload_text(self, ref: Any, fold: _SearchFold | None) -> str:
        """Decode ``ref``'s payload, preferring the one-fold snapshot.

        ``fold=None`` (no snapshot) or a snapshot miss (a ref not defined by
        a ``CellWriteEvent``) reproduces the original
        ``regenerate(record_access=False)`` read byte-for-byte.
        """
        if fold is not None:
            raw = fold.payloads.get(ref)
            if raw is not None:
                return raw.decode("utf-8", errors="replace")
        return self.store.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
            "utf-8", errors="replace"
        )

    def _first_fact_value(
        self, ref: Any, predicate: str, fold: _SearchFold | None = None
    ) -> str | None:
        """Currently-valid ``predicate`` object for ``ref``, resolved to a plain string.

        With a fold snapshot, runs the substrate's own ``TemporalLens`` over
        the cached event list — the exact lens ``store.query_facts`` delegates
        to, minus the per-call log re-materialization.
        """
        if fold is not None:
            from amplifier_data.lenses.temporal import TemporalLens

            res = TemporalLens().query(kernel=fold, subject=ref, predicate=predicate)
        else:
            res = self.store.query_facts(subject=ref, predicate=predicate)  # type: ignore[attr-defined]
        if not res.output:
            return None
        return self._payload_text(res.output[0].object, fold)

    def drawer_times(
        self, refs: Sequence[Any], *, _fold: _SearchFold | None = None
    ) -> dict[Any, str | None]:
        """Earliest ``filed_at`` per ref (T0.2), read from the one-fold snapshot.

        Reuses the SAME shared :class:`_SearchFold` a caller may already hold
        (``_fold``, mirroring :meth:`list_drawers`'s private perf seam) --
        never a per-ref ``regenerate``/re-fold (perf rationale: lines 53-95).
        When no ``_fold`` is supplied, computes its own snapshot (still ONE
        fold for the whole batch of *refs*, not one per ref). Falls back to a
        per-ref ``query_facts`` walk only for backends with no foldable
        kernel (RemoteStore/GatewayClient). A ref with no ``filed_at`` fact
        (legacy drawer, pre-T0.2) maps to ``None``.
        """
        fold = _fold if _fold is not None else self._fold_snapshot()
        out: dict[Any, str | None] = {}
        if fold is not None:
            for ref in refs:
                out[ref] = fold.filed_at.get(ref)
            return out
        s = self.store
        for ref in refs:
            res = s.query_facts(subject=ref, predicate=_FILED_AT_PREDICATE)  # type: ignore[attr-defined]
            if not res.output:
                out[ref] = None
                continue
            values = [
                s.regenerate(f.object, record_access=False).payload.decode(  # type: ignore[attr-defined]
                    "utf-8", errors="replace"
                )
                for f in res.output
            ]
            out[ref] = min(values)
        return out

    def _resolve_wing_room(
        self, ref: Any, fold: _SearchFold | None = None
    ) -> tuple[str | None, str | None]:
        """(wing, room) for a drawer ref, resolved from its direct ``scoped_to`` edges."""
        s = self.store
        if fold is not None:
            from amplifier_data.lenses.graph import GraphLens

            neighbors = GraphLens().neighbors(fold, ref, rel_type="scoped_to")
        else:
            neighbors = s.graph_neighbors(ref, rel_type="scoped_to")  # type: ignore[attr-defined]
        wing_name: str | None = None
        room_name: str | None = None
        for n in neighbors:
            label = self._payload_text(n, fold)
            if label.startswith("wing:"):
                wing_name = label[len("wing:") :]
            elif label.startswith("room:"):
                room_name = label[len("room:") :]
        return wing_name, room_name

    def list_drawers(
        self,
        *,
        wing: str | None = None,
        room: str | None = None,
        limit: int = 200,
        _fold: _SearchFold | None = None,
    ) -> list[dict[str, Any]]:
        """Scoped drawer listing for garden/status/degraded-search (§3.2, §6.2).

        Members are discovered via the scope fold (the reverse of
        ``graph_neighbors``, which walks a drawer's OWN outgoing scope edges):
        ``fold_scope(kernel).cells_in_scope(scope_ref)``. Room narrows more
        than wing when both are given. Omitting both returns every drawer
        that carries at least one scope edge (best-effort global listing --
        there is no dedicated "all drawers" index).

        Payloads and lens reads go through the one-fold snapshot when the
        caller supplies one (``_fold``, private perf seam for :meth:`search`),
        otherwise regenerated with ``record_access=False`` -- either way this
        is a read surface, not user-facing content access (D4 read-vs-fold
        boundary, mirrored from the existing verify-only reads on this seam).
        Ordering is by ref (deterministic, content-addressed) -- callers that
        need recency should look at ``has_importance``/timeline facts.
        """
        from amplifier_data.lenses._scope import fold_scope

        s = self.store
        if room is not None:
            scope_ref = self._scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._scope_ref("wing", wing)
        else:
            scope_ref = None

        scope_index = fold_scope(_fold if _fold is not None else s.kernel)  # type: ignore[attr-defined]
        if scope_ref is not None:
            member_refs = scope_index.cells_in_scope(scope_ref)
        else:
            member_refs = set(scope_index.membership.keys())

        selected_refs = sorted(member_refs)[: max(0, limit)]
        times = self.drawer_times(selected_refs, _fold=_fold)

        out: list[dict[str, Any]] = []
        for ref in selected_refs:
            content = self._payload_text(ref, _fold)
            wing_name, room_name = self._resolve_wing_room(ref, _fold)
            category = self._first_fact_value(ref, "has_category", _fold)
            importance_raw = self._first_fact_value(ref, "has_importance", _fold)
            out.append(
                {
                    "ref": ref,
                    "content": content,
                    "wing": wing_name,
                    "room": room_name,
                    "category": category,
                    "importance": float(importance_raw)
                    if importance_raw is not None
                    else None,
                    "filed_at": times.get(ref),
                }
            )
        return out

    #: Scan depth for the fully-degraded (embedder-unavailable) lexical search
    #: path (§6.2). Slow for huge wings -- acceptable for a degraded mode,
    #: documented (the design doc's own tradeoff call).
    _DEGRADED_SEARCH_SCAN_LIMIT = 1000

    def search(
        self,
        query_vector: Sequence[float] | None,
        k: int,
        *,
        wing: str | None = None,
        room: str | None = None,
        lexical_query: str | None = None,
        fusion: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid rank (§6, T1.2/D9 RRF fusion) or lexical-only (§6.2).

        ``fusion`` selects the ranking strategy:

        * ``"rrf"`` (the default whenever ``amplifier_data.lenses.bm25.``
          ``BM25Index`` is importable AND the backend has a foldable kernel):
          fuses a semantic arm (cosine, existing ``VectorLens``) and a
          lexical arm (BM25, T1.1) by Reciprocal Rank Fusion
          (``rrf = Σ_arms 1 / (60 + rank)``, rank 1-based) over the SAME
          scoped candidate set. Recovers exact-identifier queries (error
          codes, paths) that have no semantic neighbour -- the case the
          v2.0.1 cosine-then-lexical-rerank path could never surface,
          because lexical_score only reranked the vector top-``k*3``
          instead of contributing its own ranked arm.
        * ``"legacy"`` reproduces the v2.0.1
          ``0.85 * cosine + 0.15 * lexical_score`` re-rank byte-for-byte
          (:meth:`_search_legacy`) -- kept for the existing result-
          equivalence tests, and used automatically whenever BM25Index is
          not installed or *fold* is ``None`` (a remote backend with no
          foldable kernel: RRF needs the fold to build the BM25 index
          cheaply, one materialized event list per call, same as every
          other lens read in this method).

        ``query_vector=None`` (embedder not ready) still degrades in both
        modes: RRF runs the BM25 arm alone over the scoped candidates
        (:meth:`_search_rrf`); legacy falls back to a full scoped scan
        (:meth:`list_drawers`) scored purely by
        ``amplifier_module_tool_memory.embedder.lexical_score`` (§6.2).

        ``since``/``until`` (T1.3, ISO-8601 strings, inclusive bounds)
        filter the candidate set by each drawer's EARLIEST ``filed_at``
        (:meth:`drawer_times`) BEFORE scoring, in EITHER fusion mode. A
        drawer with no ``filed_at`` fact (pre-T0.2 legacy content) is
        EXCLUDED whenever either bound is given -- conservative: an
        unknown-aged drawer can never be proven to fall inside an explicit
        window. Omitting both bounds is a no-op (every candidate stays
        eligible) -- so v2.0.1 callers that never pass these are unaffected.

        Returns ``[{ref, score, content, wing, room, category, source,
        filed_at}]``, plus (RRF mode only) ``rrf`` (the fused score) and
        ``arms`` (``{"semantic": rank|None, "bm25": rank|None}``, 1-based).
        ``score`` is ALWAYS the legacy-shaped blended value (0.85*cosine +
        0.15*lexical, or lexical alone for a BM25-only/degraded hit) so
        existing downstream thresholds (the interject cosine gate, briefing
        rerank) keep their meaning regardless of fusion mode.

        Every read in this method goes through ONE log fold per call
        (:class:`_SearchFold` -- see its class docstring for the perf
        rationale this preserves: O(1) folds instead of O(reads) regenerate
        calls). The caller (the daemon dispatch layer) is responsible for
        setting the wire-level ``degraded`` flag based on whether it passed
        a real vector.
        """
        s = self.store
        scope_ref = None
        if room is not None:
            scope_ref = self._scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._scope_ref("wing", wing)
        # Snapshot AFTER the scope-cell write above, so the fold sees at least
        # everything the per-call store-surface folds used to see.
        fold = self._fold_snapshot()

        resolved_fusion = (
            fusion
            if fusion is not None
            else ("rrf" if BM25Index is not None else "legacy")
        )
        use_rrf = (
            resolved_fusion == "rrf" and BM25Index is not None and fold is not None
        )

        eligible: set[Any] | None = None
        if since is not None or until is not None:
            from amplifier_data.lenses._scope import fold_scope

            scope_index = fold_scope(fold if fold is not None else s.kernel)  # type: ignore[attr-defined]
            universe = (
                set(scope_index.cells_in_scope(scope_ref))
                if scope_ref is not None
                else set(scope_index.membership.keys())
            )
            eligible = self._temporal_eligible(universe, fold, since, until)

        if use_rrf:
            return self._search_rrf(
                query_vector,
                k,
                wing=wing,
                room=room,
                lexical_query=lexical_query,
                scope_ref=scope_ref,
                fold=fold,
                eligible=eligible,
            )
        return self._search_legacy(
            query_vector,
            k,
            wing=wing,
            room=room,
            lexical_query=lexical_query,
            scope_ref=scope_ref,
            fold=fold,
            eligible=eligible,
        )

    def _temporal_eligible(
        self,
        universe: set[Any],
        fold: _SearchFold | None,
        since: str | None,
        until: str | None,
    ) -> set[Any]:
        """T1.3: ref subset of *universe* whose EARLIEST ``filed_at`` falls in
        ``[since, until]`` (either bound optional; both given -> both apply).

        Only called by :meth:`search` when at least one bound is given.
        Undated drawers (no ``filed_at`` fact -- pre-T0.2 legacy content) are
        always EXCLUDED here: an unknown age can never be proven to satisfy
        an explicit window.
        """
        times = self.drawer_times(sorted(universe), _fold=fold)
        eligible: set[Any] = set()
        for ref, filed in times.items():
            if filed is None:
                continue
            if since is not None and filed < since:
                continue
            if until is not None and filed > until:
                continue
            eligible.add(ref)
        return eligible

    def _search_legacy(
        self,
        query_vector: Sequence[float] | None,
        k: int,
        *,
        wing: str | None,
        room: str | None,
        lexical_query: str | None,
        scope_ref: Any,
        fold: _SearchFold | None,
        eligible: set[Any] | None,
    ) -> list[dict[str, Any]]:
        """v2.0.1 hybrid-rank / lexical-only search (§6) -- see :meth:`search`
        for the full contract. Reached via ``fusion="legacy"``, or
        automatically whenever BM25Index is not installed or *fold* is
        ``None``. Preserved byte-for-byte from the pre-T1.2 implementation
        apart from the *eligible* filter (T1.3), which is a no-op set
        membership check when ``since``/``until`` were both omitted (in
        which case :meth:`search` passes ``eligible=None``).
        """
        from .embedder import lexical_score

        s = self.store
        scored: list[tuple[Any, float]] = []
        seen_refs: set[Any] = set()
        if query_vector is not None:
            if fold is not None:
                from amplifier_data.lenses.vector import VectorLens

                candidates = (
                    VectorLens()
                    .query(
                        kernel=fold,
                        vector=list(query_vector),
                        k=max(1, k * 3),
                        scope=scope_ref,
                    )
                    .output
                )
            else:
                candidates = s.query_vector(  # type: ignore[attr-defined]
                    list(query_vector), max(1, k * 3), scope=scope_ref
                )
            for ref, cosine in candidates:
                if eligible is not None and ref not in eligible:
                    continue
                content = self._payload_text(ref, fold)
                lex = lexical_score(lexical_query or "", content)
                scored.append((ref, 0.85 * cosine + 0.15 * lex))
                seen_refs.add(ref)

            # Search hardening (cold-start data-loss fix, §6 addendum): a drawer
            # filed while the embedder was not ready carries a `needs_embedding`
            # marker and has NO vector -- query_vector alone can never surface it,
            # even after the embedder warms, until the daemon's catch-up sweep
            # (daemon.py's _sweep_needs_embedding) actually runs. Union in a
            # lexical full-scan ONLY when such markers exist, so the write-to-
            # sweep window stays searchable. Vector-scored hits keep priority
            # (skipped via seen_refs); once the sweep converges, query_facts
            # returns empty and this whole block is one cheap query with no
            # scan -- the steady-state hot path is unchanged.
            if fold is not None:
                from amplifier_data.lenses.temporal import TemporalLens

                pending = TemporalLens().query(kernel=fold, predicate="needs_embedding")
            else:
                pending = s.query_facts(predicate="needs_embedding")  # type: ignore[attr-defined]
            if pending.success and pending.output:
                for drawer in self.list_drawers(
                    wing=wing,
                    room=room,
                    limit=self._DEGRADED_SEARCH_SCAN_LIMIT,
                    _fold=fold,
                ):
                    ref = drawer["ref"]
                    if ref in seen_refs:
                        continue
                    if eligible is not None and ref not in eligible:
                        continue
                    scored.append(
                        (ref, lexical_score(lexical_query or "", drawer["content"]))
                    )
                    seen_refs.add(ref)
        else:
            for drawer in self.list_drawers(
                wing=wing,
                room=room,
                limit=self._DEGRADED_SEARCH_SCAN_LIMIT,
                _fold=fold,
            ):
                if eligible is not None and drawer["ref"] not in eligible:
                    continue
                scored.append(
                    (
                        drawer["ref"],
                        lexical_score(lexical_query or "", drawer["content"]),
                    )
                )

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        top = scored[: max(0, k)]
        times = self.drawer_times([ref for ref, _score in top], _fold=fold)
        results: list[dict[str, Any]] = []
        for ref, score in top:
            content = self._payload_text(ref, fold)
            wing_name, room_name = self._resolve_wing_room(ref, fold)
            category = self._first_fact_value(ref, "has_category", fold)
            source = self._first_fact_value(ref, "has_source", fold)
            results.append(
                {
                    "ref": ref,
                    "score": score,
                    "content": content,
                    "wing": wing_name,
                    "room": room_name,
                    "category": category,
                    "source": source,
                    "filed_at": times.get(ref),
                }
            )
        return results

    def _search_rrf(
        self,
        query_vector: Sequence[float] | None,
        k: int,
        *,
        wing: str | None,
        room: str | None,
        lexical_query: str | None,
        scope_ref: Any,
        fold: _SearchFold | None,
        eligible: set[Any] | None,
    ) -> list[dict[str, Any]]:
        """T1.2 (D9): RRF fusion of a semantic arm (cosine) and a lexical arm
        (BM25) over the scoped candidate set -- see :meth:`search` for the
        full contract. Only called when *fold* and ``BM25Index`` are both
        available (:meth:`search` already resolved that).

        The BM25 index is built (incrementally, idempotently) from EVERY
        drawer ref visible in this fold's scope membership -- not just the
        refs scoped to this particular query -- so ``BM25Index``'s global
        IDF stays meaningful across searches touching different wings/rooms,
        and `.add()` for an already-indexed ref is a cheap no-op (BM25Index
        docstring). BM25 *scoring* is still filtered to this call's scoped
        (and, if given, temporally eligible) candidates via ``candidates=``.
        """
        from amplifier_data.lenses._scope import fold_scope

        from .embedder import lexical_score

        assert fold is not None
        assert BM25Index is not None

        scope_index = fold_scope(fold)
        all_drawer_refs = set(scope_index.membership.keys())
        scoped_refs = (
            set(scope_index.cells_in_scope(scope_ref))
            if scope_ref is not None
            else all_drawer_refs
        )
        candidate_refs = scoped_refs if eligible is None else (scoped_refs & eligible)

        # Keep the persistent index current: idempotent per ref, so repeat
        # calls across the store's lifetime only ever tokenize NEW drawers.
        index = self._bm25_index
        for ref in sorted(all_drawer_refs):
            index.add(ref, self._payload_text(ref, fold))

        n_pool = max(3 * k, 50)

        semantic_rank: dict[Any, int] = {}
        cosine_by_ref: dict[Any, float] = {}
        if query_vector is not None:
            from amplifier_data.lenses.vector import VectorLens

            # When a temporal filter narrowed the eligible set, request a
            # wider vector pool so an eligible-but-lower-cosine candidate
            # is not crowded out of the top-n_pool by ineligible ones.
            vector_pool_k = (
                max(n_pool, len(scoped_refs)) if eligible is not None else n_pool
            )
            raw_candidates = (
                VectorLens()
                .query(
                    kernel=fold,
                    vector=list(query_vector),
                    k=max(1, vector_pool_k),
                    scope=scope_ref,
                )
                .output
            )
            rank = 0
            for ref, cosine in raw_candidates:
                if eligible is not None and ref not in eligible:
                    continue
                rank += 1
                if rank > n_pool:
                    break
                semantic_rank[ref] = rank
                cosine_by_ref[ref] = cosine

        bm25_hits = index.score(
            lexical_query or "", candidates=candidate_refs, k=n_pool
        )
        bm25_rank: dict[Any, int] = {
            ref: i + 1 for i, (ref, _score) in enumerate(bm25_hits)
        }

        fused: list[tuple[Any, float]] = []
        for ref in set(semantic_rank) | set(bm25_rank):
            rrf = 0.0
            if ref in semantic_rank:
                rrf += 1.0 / (_RRF_K + semantic_rank[ref])
            if ref in bm25_rank:
                rrf += 1.0 / (_RRF_K + bm25_rank[ref])
            fused.append((ref, rrf))
        fused.sort(key=lambda pair: (-pair[1], pair[0]))
        top = fused[: max(0, k)]

        times = self.drawer_times([ref for ref, _rrf in top], _fold=fold)
        results: list[dict[str, Any]] = []
        for ref, rrf in top:
            content = self._payload_text(ref, fold)
            wing_name, room_name = self._resolve_wing_room(ref, fold)
            category = self._first_fact_value(ref, "has_category", fold)
            source = self._first_fact_value(ref, "has_source", fold)
            if ref in cosine_by_ref:
                blended = 0.85 * cosine_by_ref[ref] + 0.15 * lexical_score(
                    lexical_query or "", content
                )
            else:
                blended = lexical_score(lexical_query or "", content)
            results.append(
                {
                    "ref": ref,
                    "score": blended,
                    "rrf": rrf,
                    "arms": {
                        "semantic": semantic_rank.get(ref),
                        "bm25": bm25_rank.get(ref),
                    },
                    "content": content,
                    "wing": wing_name,
                    "room": room_name,
                    "category": category,
                    "source": source,
                    "filed_at": times.get(ref),
                }
            )
        return results

    def read_diary(self, *, agent_name: str, last_n: int = 10) -> list[dict[str, Any]]:
        """Cells under scope ``agent:{name}``, SeqPos-ordered, newest last (§3.2).

        Mirrors :meth:`list_drawers`'s use of the scope fold for membership,
        then orders by each cell's OWN defining ``CellWriteEvent`` position in
        the log (SeqPos) -- the fold does not carry position, so this walks
        ``kernel.all_events()`` once to build a ``ref -> first SeqPos`` map.
        """
        from amplifier_data.lenses._scope import fold_scope
        from amplifier_data.models import CellWriteEvent

        s = self.store
        scope_ref = self._scope_ref("agent", agent_name)
        member_refs = fold_scope(s.kernel).cells_in_scope(scope_ref)  # type: ignore[attr-defined]

        order: dict[Any, int] = {}
        for pos, ev in s.kernel.all_events():  # type: ignore[attr-defined]
            if isinstance(ev, CellWriteEvent):
                ref = ev.cell_ref()
                if ref in member_refs and ref not in order:
                    order[ref] = pos

        ordered_refs = sorted(member_refs, key=lambda r: order.get(r, 0))
        tail = ordered_refs[-max(0, last_n) :] if last_n > 0 else []

        entries: list[dict[str, Any]] = []
        for ref in tail:
            entry_text = s.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            _, topic = self._resolve_wing_room(ref)
            entries.append(
                {
                    "ref": ref,
                    "entry": entry_text,
                    "topic": topic or "general",
                    "seq_pos": order.get(ref, 0),
                }
            )
        return entries

    def kg_stats(self) -> dict[str, int]:
        """``{facts, entities}`` over anchor-cell KG facts (§5.4 ``kg_stats`` tool).

        Counts currently-valid facts whose subject OR object is an
        ``entity:``-prefixed anchor cell (:meth:`_anchor`'s convention), via
        the existing ``query_facts`` read surface (D4: no AccessEvents).
        ``entities`` is the number of distinct anchor refs seen as either a
        subject or an object of a currently-valid fact.
        """
        s = self.store
        res = s.query_facts(subject=None, predicate=None)  # type: ignore[attr-defined]
        facts = 0
        entities: set[Any] = set()
        for fact in res.output:
            subj_text = s.regenerate(fact.subject, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            obj_text = s.regenerate(fact.object, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            subj_is_anchor = subj_text.startswith("entity:")
            obj_is_anchor = obj_text.startswith("entity:")
            if not (subj_is_anchor or obj_is_anchor):
                continue
            facts += 1
            if subj_is_anchor:
                entities.add(fact.subject)
            if obj_is_anchor:
                entities.add(fact.object)
        return {"facts": facts, "entities": len(entities)}

    def status(self) -> dict[str, Any]:
        """``{drawers, wings, kg_facts}`` overview (§5.4 ``status`` tool).

        ``drawers`` counts distinct cell refs carrying at least one
        ``scoped_to`` (wing/room) edge -- exactly the population
        :meth:`list_drawers` (unscoped) would enumerate. ``wings`` lists the
        distinct wing names seen across those drawers' scope edges. The
        daemon dispatch layer merges in ``embedder``/``durable``/``path``,
        which this seam has no reference to.
        """
        from amplifier_data.lenses._scope import fold_scope

        s = self.store
        scope_index = fold_scope(s.kernel)  # type: ignore[attr-defined]
        drawers = len(scope_index.membership)
        wings: set[str] = set()
        for scopes in scope_index.membership.values():
            for scope_ref in scopes:
                label = s.regenerate(scope_ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                    "utf-8", errors="replace"
                )
                if label.startswith("wing:"):
                    wings.add(label[len("wing:") :])
        kg = self.kg_stats()
        return {"drawers": drawers, "wings": sorted(wings), "kg_facts": kg["facts"]}
