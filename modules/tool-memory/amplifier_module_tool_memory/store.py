"""
MemoryStore — the storage seam for the consolidation pipeline.

The cold-path ``curate.dot`` pipeline produces consolidated "cells" and writes
them through a ``MemoryStore``. ``NativeMemoryStore`` is the ONE store
now (native cutover, B2, the native-cutover design history) --
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

import json
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

#: P2 (T2.1/T2.7, D12) -- reuse amplifier-data's convergent-integrity edge
#: names/conventions for an unresolved ``conflicts_with`` contradiction when
#: the library is new enough to export them; otherwise fall back to our own
#: ``@memory:`` namespace. Either way a tension is an ordinary cell with
#: reserved-namespace edges (never a gate on the write) -- see
#: amplifier_data.integrity's module docstring for the pattern this mirrors.
try:
    from amplifier_data.integrity import (
        CONFLICTS_WITH as _TENSION_CONFLICTS_WITH,
    )
    from amplifier_data.integrity import (
        IN_TENSION as _TENSION_IN_TENSION,
    )
    from amplifier_data.integrity import (
        TENSION as _TENSION_EDGE,
    )
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    _TENSION_EDGE = "@memory:tension"
    _TENSION_IN_TENSION = "@memory:in_tension"
    _TENSION_CONFLICTS_WITH = "@memory:conflicts_with"

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

#: P2 (T2.1) -- L2 fact model constants (design \u00a72 data model, \u00a74 T2.1;
#: context/reflection-rubric.md's 15-80 word guidance is a distiller-facing
#: *target*; 3-120 is the mechanism's hard gate).
_FACT_TYPES = frozenset(
    {"world", "experience", "preference", "procedure", "correction"}
)
_FACT_MIN_WORDS = 3
_FACT_MAX_WORDS = 120

#: Reserved ``@memory:`` edge/fact namespace (mirrors amplifier_data.integrity's
#: ``@integrity:``/``@plan:`` reserved-namespace convention: user code must
#: never mint these).
_MEMORY_CURRENT = "@memory:current"
_MEMORY_DERIVED_FROM = "@memory:derived_from"
_MEMORY_SUPERSEDES = "@memory:supersedes"
_MEMORY_PREDICATE = "@memory:predicate"
_MEMORY_JOB_STATE = "@memory:job_state"
_MEMORY_JOB_PRODUCED = "@memory:job_produced"

#: Scale-defect-1 fix (see docstring on ``_SearchFold``/``_resolve_filing``):
#: content addressing makes identical drawer text filed in different
#: wings/rooms share ONE ref, so per-filer facts (``has_source`` etc.)
#: resolved arbitrarily regardless of query scope. Every :meth:`file` /
#: :meth:`reflection_job_add` call now ALSO writes one small
#: content-addressed FILING cell recording THAT ONE filing's own
#: wing/room/source/category/filed_at/session/commit, linked via this edge.
_MEMORY_FILED_AS = "@memory:filed_as"

#: P3 (T3.1/T3.3, D19 -- index is mechanism-first) reserved ``@memory:``
#: predicates. ``_MEMORY_CURRENT_INDEX`` lives on the ROOM SCOPE CELL
#: (subject) pointing at the currently-curated index cell for that room --
#: mirrors the has_importance UPDATE pattern (invalidate-old + assert-new).
#: ``_MEMORY_CURRENT_ANSWER`` is the same pattern on a standing QUESTION
#: cell, pointing at its current answer cell. ``_MEMORY_CITES`` is the
#: navigation-to-evidence edge (index cell / standing-answer cell -> fact
#: refs) -- never a provenance edge (that's ``_MEMORY_DERIVED_FROM``).
_MEMORY_CITES = "@memory:cites"
_MEMORY_CURRENT_INDEX = "@memory:current_index"
_MEMORY_CURRENT_ANSWER = "@memory:current_answer"

#: Time/provenance facts on a fact cell (D8: consumer-supplied facts, mirrors
#: the drawer predicates above).
_RECORDED_AT_PREDICATE = "recorded_at"
_EXPIRED_AT_PREDICATE = "expired_at"
_OBSERVED_AT_PREDICATE = "observed_at"
_BUILT_AT_PREDICATE = "built_at"
_BUILT_FROM_COUNT_PREDICATE = "built_from_count"
_ANSWERED_AT_PREDICATE = "answered_at"

#: Content-addressed boolean marker cell -- always the SAME ref (idempotent
#: ``write_cell``), so a later supersede can invalidate exactly the object
#: cell an earlier ``fact_add`` asserted for ``@memory:current`` without
#: having to look it up first.
_TRUE_CELL_BYTES = b"true"

#: T1.4 hot-path latency gate (design \u00a76 measurement bar): search p95 on a
#: 5k-drawer synthetic store must stay under this budget. A module constant
#: (not a magic number in the test) so the budget is visible and tunable in
#: one place.
SEARCH_P95_BUDGET_MS = 300

#: P3 (T3.1, design \u00a72 data model) -- L3 index length caps and default
#: staleness gate. A curated index cell is served as-is (``source:"curated"``)
#: while the room has accumulated at most this many facts/drawers since it
#: was built; beyond that, reads fall back to a computed-on-read derived
#: index (mechanism-first, D19) until a curator overwrites it.
_INDEX_ABSTRACT_MAX_CHARS = 256
_INDEX_OVERVIEW_MAX_CHARS = 4000
_INDEX_STALE_AFTER_DEFAULT = 10


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
        from amplifier_data.lenses._scope import SCOPED_TO
        from amplifier_data.lenses.temporal import INVALIDATE_PREFIX
        from amplifier_data.lenses.vector import EMBEDDING_OF
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
        # Kept as the LEGACY (pre-filing) fallback -- see `filings` below,
        # which is scope-aware and preferred whenever it exists.
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

        # Scale fix (perf/attribution-and-fold-once): a THIRD pass over the
        # SAME materialized event list precomputes everything else a
        # per-hit metadata lookup used to re-fold the log for:
        #   - scope_membership: ref -> {scope_ref, ...}, the exact data
        #     `fold_scope`/`GraphLens.neighbors(rel_type="scoped_to")` used
        #     to recompute from scratch on every call.
        #   - triple_history / by_subject_predicate / by_predicate_object:
        #     mirrors TemporalLens's own fold exactly (assert/invalidate per
        #     (subject, predicate, object) triple, SeqPos-ordered), so
        #     `current_objects`/`objects_pointing_to` below reproduce
        #     TemporalLens.current_facts() without a second event-log walk.
        #   - filings: drawer ref -> [(filing_ref, filing_body), ...], the
        #     new per-filing attribution record (see module-level
        #     `_MEMORY_FILED_AS`) written by `file()`/`reflection_job_add`.
        scope_membership: dict[Any, set[Any]] = {}
        triple_history: dict[tuple[Any, str, Any], list[tuple[Any, str]]] = {}
        by_subject_predicate: dict[tuple[Any, str], list[Any]] = {}
        by_predicate_object: dict[tuple[str, Any], list[Any]] = {}
        filed_as_edges: list[tuple[Any, Any]] = []
        for seq_pos, ev in self._events:
            if not isinstance(ev, RelationshipEvent):
                continue
            etype = ev.type
            if etype == SCOPED_TO:
                scope_membership.setdefault(ev.from_ref, set()).add(ev.to_ref)
                continue
            if etype == EMBEDDING_OF:
                continue
            if etype == _MEMORY_FILED_AS:
                filed_as_edges.append((ev.from_ref, ev.to_ref))
            if etype.startswith(INVALIDATE_PREFIX):
                predicate = etype[len(INVALIDATE_PREFIX) :]
                op = "invalidate"
            else:
                predicate = etype
                op = "assert"
            triple_history.setdefault((ev.from_ref, predicate, ev.to_ref), []).append(
                (seq_pos, op)
            )
            sp_objs = by_subject_predicate.setdefault((ev.from_ref, predicate), [])
            if ev.to_ref not in sp_objs:
                sp_objs.append(ev.to_ref)
            po_subs = by_predicate_object.setdefault((predicate, ev.to_ref), [])
            if ev.from_ref not in po_subs:
                po_subs.append(ev.from_ref)
        self.scope_membership: dict[Any, set[Any]] = scope_membership
        self._triple_history = triple_history
        self._by_subject_predicate = by_subject_predicate
        self._by_predicate_object = by_predicate_object

        filings: dict[Any, list[tuple[Any, dict[str, Any]]]] = {}
        for drawer_ref, filing_ref in filed_as_edges:
            raw = payloads.get(filing_ref)
            if raw is None:
                continue
            try:
                body = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, TypeError):
                continue
            if not isinstance(body, dict) or body.get("kind") != "filing":
                continue
            filings.setdefault(drawer_ref, []).append((filing_ref, body))
        self.filings: dict[Any, list[tuple[Any, dict[str, Any]]]] = filings

    def all_events(self) -> Any:
        """The read accessor the fold lenses use (mirrors ``StorageKernel``)."""
        return self._events

    def scope_index(self) -> Any:
        """A :class:`ScopeIndex`-shaped view over the precomputed
        ``scope_membership`` -- callers that used to call
        ``fold_scope(fold)`` (itself a full re-walk of the event list) get
        the same shape from data this fold already built once."""
        from amplifier_data.lenses._scope import ScopeIndex

        return ScopeIndex(
            membership={k: frozenset(v) for k, v in self.scope_membership.items()}
        )

    def current_objects(self, subject: Any, predicate: str) -> list[Any]:
        """Currently-valid objects of (*subject*, *predicate*), sorted --
        reproduces ``TemporalLens.current_facts(kernel, subject, predicate)``
        exactly, without re-walking the event list a second time."""
        out: list[Any] = []
        for obj in self._by_subject_predicate.get((subject, predicate), []):
            events = self._triple_history[(subject, predicate, obj)]
            if max(events, key=lambda e: e[0])[1] == "assert":
                out.append(obj)
        return sorted(out)

    def objects_pointing_to(self, obj: Any, predicate: str) -> list[Any]:
        """Currently-valid subjects of (*subject*, *predicate*, *obj*) --
        the reverse of :meth:`current_objects`, used for e.g.
        ``@memory:supersedes`` lookups by object."""
        out: list[Any] = []
        for subj in self._by_predicate_object.get((predicate, obj), []):
            events = self._triple_history[(subj, predicate, obj)]
            if max(events, key=lambda e: e[0])[1] == "assert":
                out.append(subj)
        return sorted(out)


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
            # Scale-defect-1 fix (D-scale-1, _MEMORY_FILED_AS): staged in the
            # SAME batch/commit as the drawer -- one append_batch per file()
            # call, same as before this fix. The filing payload's "drawer"
            # field is write-only bookkeeping (never read back by any
            # resolution path in this module; attribution reads travel the
            # `ref -> filing` edge, never the reverse), so embedding the
            # PRE-commit `ref` here is safe even on a backend where the
            # pre-commit token differs from the final content address
            # (GatewayClient) -- unlike a field this module actually reads
            # back, which would require a second post-commit write.
            filing_ref = b.write_cell(
                self._filing_payload(
                    drawer_ref=ref,
                    wing=wing,
                    room=room,
                    source=source,
                    category=category,
                    filed_at=filed_at,
                    session_id=session_id,
                    commit=commit,
                )
            )
            b.assert_fact(ref, _MEMORY_FILED_AS, filing_ref)
            commit_result = b.commit()
            ref = _resolve_batch_ref(commit_result, ref)
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
            # Scale-defect-1 fix: same FILING cell as the atomic branch above
            # (see its comment) -- the sequential path is already one call
            # per write, so this adds no extra commit round-trip either.
            filing_ref = s.write_cell(  # type: ignore[attr-defined]
                self._filing_payload(
                    drawer_ref=ref,
                    wing=wing,
                    room=room,
                    source=source,
                    category=category,
                    filed_at=filed_at,
                    session_id=session_id,
                    commit=commit,
                )
            )
            s.assert_fact(ref, _MEMORY_FILED_AS, filing_ref)  # type: ignore[attr-defined]
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

    @staticmethod
    def _filing_payload(
        *,
        drawer_ref: Any,
        wing: str,
        room: str,
        source: str,
        category: str | None,
        filed_at: str,
        session_id: str | None,
        commit: str | None,
    ) -> bytes:
        """Content-addressed FILING cell payload for one filing of
        *drawer_ref* (scale defect 1 fix, see ``_MEMORY_FILED_AS`` /
        :meth:`_resolve_filing`). ``source``/``category`` are normalized the
        same way the drawer's own has_source/has_category facts are (empty
        ``source`` -> ``None``, so the legacy-fallback path sees the
        identical value either way).

        ``drawer`` is write-only bookkeeping: no resolution path in this
        module reads it back (attribution always travels the ``ref ->
        filing`` edge, never the reverse), so it is safe to embed a
        PRE-commit ref value here even on a backend where the pre-commit
        token differs from the final content address (GatewayClient) --
        letting every caller stage this cell in the SAME batch/commit as
        the drawer itself, one append_batch per write, same as before this
        fix (a field this module actually read back would need a second,
        post-commit write instead -- see :meth:`_file_filing`, used only by
        :meth:`reflection_job_add`, whose span ref is already resolved).
        """
        body = {
            "kind": "filing",
            "drawer": drawer_ref,
            "wing": wing,
            "room": room,
            "source": source or None,
            "category": category,
            "filed_at": filed_at,
            "session": session_id,
            "commit": commit,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _file_filing(
        self,
        *,
        drawer_ref: Any,
        wing: str,
        room: str,
        source: str,
        category: str | None,
        filed_at: str,
        session_id: str | None,
        commit: str | None,
    ) -> Any:
        """Write the FILING cell + ``@memory:filed_as`` edge for one filing
        of an ALREADY-resolved *drawer_ref* -- used by
        :meth:`reflection_job_add`, whose span ref must be resolved first (a
        second small commit; see :meth:`_filing_payload`'s docstring for why
        :meth:`file` itself instead folds this into the drawer's own batch).
        """
        payload = self._filing_payload(
            drawer_ref=drawer_ref,
            wing=wing,
            room=room,
            source=source,
            category=category,
            filed_at=filed_at,
            session_id=session_id,
            commit=commit,
        )
        s = self.store
        if self._supports_atomic_update():
            b = s.write_batch()  # type: ignore[attr-defined]
            filing_ref = b.write_cell(payload)
            b.assert_fact(drawer_ref, _MEMORY_FILED_AS, filing_ref)
            commit_result = b.commit()
            filing_ref = _resolve_batch_ref(commit_result, filing_ref)
        else:
            filing_ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.assert_fact(drawer_ref, _MEMORY_FILED_AS, filing_ref)  # type: ignore[attr-defined]
        return filing_ref

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
    # Native read surfaces (§3.2 of the native-cutover design history)
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
            out = fold.current_objects(ref, predicate)
        else:
            res = self.store.query_facts(subject=ref, predicate=predicate)  # type: ignore[attr-defined]
            out = [f.object for f in res.output]
        if not out:
            return None
        return self._payload_text(out[0], fold)

    def drawer_times(
        self,
        refs: Sequence[Any],
        *,
        _fold: _SearchFold | None = None,
        wing: str | None = None,
    ) -> dict[Any, str | None]:
        """``filed_at`` per ref, scope-aware when *wing* is given (T0.2, scale
        fix: drawer_times per-wing earliest).

        Prefers the filing matching *wing* (see :meth:`_resolve_filing`) --
        identical content filed into several wings/rooms shares ONE ref, so
        the GLOBAL earliest ``filed_at`` used before this fix could report a
        time from a different wing's filing entirely. Falls back to the
        legacy global-earliest-fact value for a drawer with no filing cell
        (pre-fix content). Reuses the SAME shared :class:`_SearchFold` a
        caller may already hold (``_fold``) -- never a per-ref
        ``regenerate``/re-fold. A ref with neither a filing nor a
        ``filed_at`` fact (legacy drawer, pre-T0.2) maps to ``None``.
        """
        fold = _fold if _fold is not None else self._fold_snapshot()
        out: dict[Any, str | None] = {}
        for ref in refs:
            filing = self._resolve_filing(ref, fold, wing=wing, room=None)
            if filing is not None:
                out[ref] = filing.get("filed_at")
                continue
            if fold is not None:
                out[ref] = fold.filed_at.get(ref)
                continue
            s = self.store
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

    def _filings_for(
        self, ref: Any, fold: _SearchFold | None
    ) -> list[tuple[Any, dict[str, Any]]]:
        """Every ``{"kind": "filing", ...}`` cell linked from *ref* via
        ``@memory:filed_as``, as ``[(filing_ref, body), ...]``."""
        if fold is not None:
            return fold.filings.get(ref, [])
        s = self.store
        res = s.query_facts(subject=ref, predicate=_MEMORY_FILED_AS)  # type: ignore[attr-defined]
        out: list[tuple[Any, dict[str, Any]]] = []
        for f in res.output:
            try:
                body = json.loads(self._payload_text(f.object, None))
            except (ValueError, TypeError):
                continue
            if isinstance(body, dict) and body.get("kind") == "filing":
                out.append((f.object, body))
        return out

    def _resolve_filing(
        self,
        ref: Any,
        fold: _SearchFold | None,
        *,
        wing: str | None,
        room: str | None,
    ) -> dict[str, Any] | None:
        """Scale-defect-1 fix: resolve *ref*'s attribution (source/wing/room/
        category/filed_at/session/commit) to the ONE filing matching the
        query's scope, instead of an arbitrary per-predicate fact shared
        across every wing that ever filed this content.

        Scoped query (*wing* given): among filings whose wing matches (and
        room too, when *room* is given), picks the EARLIEST ``filed_at``
        (filing ref ascending breaks ties). Returns ``None`` -- never a
        wrong-scope filing -- when *ref* has filings but none match this
        scope (a legacy drawer scoped here before this fix landed, now also
        filed elsewhere); callers fall back to the pre-fix per-predicate
        resolution in that case. Unscoped query: the globally earliest
        filing. Returns ``None`` when *ref* carries no filing cell at all
        (a legacy drawer, pre-scale-fix).
        """
        filings = self._filings_for(ref, fold)
        if not filings:
            return None
        if wing is not None:
            scoped = [
                (fref, body)
                for fref, body in filings
                if body.get("wing") == wing
                and (room is None or body.get("room") == room)
            ]
            if not scoped:
                return None
            pool = scoped
        else:
            pool = filings
        _fref, body = min(
            pool, key=lambda item: (item[1].get("filed_at") or "", item[0])
        )
        return body

    def _resolve_hit_meta(
        self,
        ref: Any,
        fold: _SearchFold | None,
        *,
        wing: str | None,
        room: str | None,
    ) -> dict[str, Any]:
        """``{"wing","room","source","category","filed_at"}`` for one drawer
        hit (search/list_drawers), scope-aware (see :meth:`_resolve_filing`);
        falls back to the pre-fix per-predicate facts + first-matching scope
        edge for a legacy drawer with no filing."""
        filing = self._resolve_filing(ref, fold, wing=wing, room=room)
        if filing is not None:
            return {
                "wing": filing.get("wing"),
                "room": filing.get("room"),
                "source": filing.get("source"),
                "category": filing.get("category"),
                "filed_at": filing.get("filed_at"),
            }
        wing_name, room_name = self._resolve_wing_room(ref, fold)
        return {
            "wing": wing_name,
            "room": room_name,
            "source": self._first_fact_value(ref, "has_source", fold),
            "category": self._first_fact_value(ref, "has_category", fold),
            "filed_at": self.drawer_times([ref], _fold=fold, wing=wing).get(ref),
        }

    def _resolve_wing_room(
        self, ref: Any, fold: _SearchFold | None = None
    ) -> tuple[str | None, str | None]:
        """(wing, room) for a drawer ref, resolved from its direct ``scoped_to``
        edges. LEGACY/fallback path only: a ref shared by multiple wings (content
        addressing) has multiple wing/room edges and this picks one
        deterministically, but not scope-aware -- see :meth:`_resolve_filing`
        for the scope-aware attribution used by search/list_drawers hits."""
        if fold is not None:
            # Precomputed once per request (perf/attribution-and-fold-once) --
            # no GraphLens re-fold of the whole event list per ref.
            neighbors: Any = sorted(fold.scope_membership.get(ref, ()))
        else:
            s = self.store
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

        scope_index = (
            _fold.scope_index() if _fold is not None else fold_scope(s.kernel)  # type: ignore[attr-defined]
        )
        if scope_ref is not None:
            member_refs = scope_index.cells_in_scope(scope_ref)
        else:
            member_refs = set(scope_index.membership.keys())

        selected_refs = sorted(member_refs)[: max(0, limit)]

        out: list[dict[str, Any]] = []
        for ref in selected_refs:
            content = self._payload_text(ref, _fold)
            meta = self._resolve_hit_meta(ref, _fold, wing=wing, room=room)
            importance_raw = self._first_fact_value(ref, "has_importance", _fold)
            out.append(
                {
                    "ref": ref,
                    "content": content,
                    "wing": meta["wing"],
                    "room": meta["room"],
                    "category": meta["category"],
                    "importance": float(importance_raw)
                    if importance_raw is not None
                    else None,
                    "filed_at": meta["filed_at"],
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
        layers: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid rank (§6, T1.2/D9 RRF fusion) or lexical-only (§6.2).

        ``layers`` (T2.2, default ``("fact", "drawer")`` when omitted) picks
        which cell kinds are eligible hits. Fact cells already share the same
        BM25/vector candidate pool as drawers (both are scoped and indexed
        identically) -- this only decides which resulting hits are kept,
        relabels each with ``layer``, and (for a fact hit) replaces the raw
        JSON payload's ``content`` with the fact's own ``text`` and adds
        ``derived_from``. Non-current facts are always excluded. Internally
        widens the candidate pool whenever a layer might be filtered out, so
        filtering never starves the caller's requested result count.

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
        layer_set = set(layers) if layers is not None else {"fact", "drawer"}
        # Only widen the internal pool when a layer is actually excluded
        # (facts and drawers share one candidate pool, so a restricted
        # request could otherwise under-fill after filtering). The default
        # (both layers) requests EXACTLY k, same as pre-P2 -- T1.4's p95
        # budget is measured against this default path and must not pay
        # for a widening it never needed (the internal RRF/legacy engines
        # already widen their OWN sub-pools proportionally to whatever k
        # they're given, so requesting a larger k here compounds).
        pool_k = k if layer_set == {"fact", "drawer"} else max(k * 3, 50)

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

            scope_index = (
                fold.scope_index() if fold is not None else fold_scope(s.kernel)  # type: ignore[attr-defined]
            )
            universe = (
                set(scope_index.cells_in_scope(scope_ref))
                if scope_ref is not None
                else set(scope_index.membership.keys())
            )
            eligible = self._temporal_eligible(universe, fold, since, until)

        if use_rrf:
            raw = self._search_rrf(
                query_vector,
                pool_k,
                wing=wing,
                room=room,
                lexical_query=lexical_query,
                scope_ref=scope_ref,
                fold=fold,
                eligible=eligible,
            )
        else:
            raw = self._search_legacy(
                query_vector,
                pool_k,
                wing=wing,
                room=room,
                lexical_query=lexical_query,
                scope_ref=scope_ref,
                fold=fold,
                eligible=eligible,
            )

        filtered: list[dict[str, Any]] = []
        for hit in raw:
            kind = self._classify_ref(hit["ref"], fold)
            layer = "fact" if kind == "fact" else "drawer"
            if layer not in layer_set:
                continue
            if layer == "fact":
                if not self._is_current(hit["ref"], fold):
                    continue
                try:
                    body = json.loads(hit["content"])
                except (ValueError, TypeError):
                    body = {}
                hit = {**hit, "content": body.get("text", "")}
                hit["derived_from"] = self._derived_from_refs(hit["ref"], fold)
            hit["layer"] = layer
            filtered.append(hit)
            if len(filtered) >= max(0, k):
                break
        return filtered

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
        results: list[dict[str, Any]] = []
        for ref, score in top:
            content = self._payload_text(ref, fold)
            meta = self._resolve_hit_meta(ref, fold, wing=wing, room=room)
            results.append(
                {
                    "ref": ref,
                    "score": score,
                    "content": content,
                    "wing": meta["wing"],
                    "room": meta["room"],
                    "category": meta["category"],
                    "source": meta["source"],
                    "filed_at": meta["filed_at"],
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

        results: list[dict[str, Any]] = []
        for ref, rrf in top:
            content = self._payload_text(ref, fold)
            meta = self._resolve_hit_meta(ref, fold, wing=wing, room=room)
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
                    "wing": meta["wing"],
                    "room": meta["room"],
                    "category": meta["category"],
                    "source": meta["source"],
                    "filed_at": meta["filed_at"],
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

    # ------------------------------------------------------------------
    # P2 -- L2 facts (T2.1/T2.2/T2.7, D12) and the reflection job queue
    # (D10). See docs/plans/2026-09-27-memory-layers-design.md \u00a72/\u00a74 and
    # context/reflection-rubric.md for the policy this mechanism serves.
    # ------------------------------------------------------------------

    def _classify_ref(self, ref: Any, fold: _SearchFold | None = None) -> str | None:
        """``"drawer"`` | ``"fact"`` | ``"reflection_job"`` | ``None``.

        ``None`` means *ref* carries no scope edge at all (not a known
        drawer/fact/job -- e.g. a scope cell or an anchor cell). A scoped
        cell whose payload does not decode as one of our ``{"kind": ...}``
        envelopes is a plain verbatim drawer (the common case, and the only
        shape that existed before P2).
        """
        wing_name, room_name = self._resolve_wing_room(ref, fold)
        if wing_name is None and room_name is None:
            return None
        text = self._payload_text(ref, fold)
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return "drawer"
        if isinstance(obj, dict) and obj.get("kind") in (
            "fact",
            "reflection_job",
            "index",
            "standing_question",
            "standing_answer",
        ):
            return str(obj["kind"])
        return "drawer"

    def _is_current(self, ref: Any, fold: _SearchFold | None = None) -> bool:
        """Whether *ref* carries a currently-valid ``@memory:current`` fact.

        Accepts an optional one-fold snapshot (P3, T3.1/T3.3) so callers
        already holding a :class:`_SearchFold` (:meth:`index`/:meth:`standing`)
        never trigger a second ``kernel.all_events()`` materialization.
        """
        if fold is not None:
            # perf/attribution-and-fold-once: reuse the fold's own precomputed
            # triple history -- no second TemporalLens re-fold of the log.
            return bool(fold.current_objects(ref, _MEMORY_CURRENT))
        s = self.store
        res = s.query_facts(subject=ref, predicate=_MEMORY_CURRENT)  # type: ignore[attr-defined]
        return bool(res.output)

    def _derived_from_refs(
        self, ref: Any, fold: _SearchFold | None = None
    ) -> list[Any]:
        if fold is not None:
            return fold.current_objects(ref, _MEMORY_DERIVED_FROM)
        s = self.store
        res = s.query_facts(subject=ref, predicate=_MEMORY_DERIVED_FROM)  # type: ignore[attr-defined]
        return [f.object for f in res.output]

    def _current_fact_refs(
        self, ref: Any, predicate: str, fold: _SearchFold | None = None
    ) -> list[Any]:
        """Object refs of every currently-valid *predicate* fact on *ref*.

        Generalizes :meth:`_derived_from_refs` for any reserved ``@memory:``
        predicate whose object is itself a meaningful cell ref (not a plain
        decoded value) -- used by the P3 index/standing machinery for
        ``@memory:current_index``, ``@memory:current_answer`` and
        ``@memory:cites``.
        """
        if fold is not None:
            return fold.current_objects(ref, predicate)
        res = self.store.query_facts(subject=ref, predicate=predicate)  # type: ignore[attr-defined]
        return [f.object for f in res.output]

    def _superseded_by(self, ref: Any, fold: _SearchFold | None = None) -> list[Any]:
        """Facts whose ``@memory:supersedes`` currently points AT *ref*."""
        if fold is not None:
            return fold.objects_pointing_to(ref, _MEMORY_SUPERSEDES)
        s = self.store
        res = s.query_facts(predicate=_MEMORY_SUPERSEDES)  # type: ignore[attr-defined]
        return [f.subject for f in res.output if f.object == ref]

    def _current_facts_with_predicate(
        self, predicate: str, *, wing: str, exclude: Any
    ) -> list[Any]:
        """T2.7: currently-valid facts in *wing* asserting the same
        single-valued *predicate*, excluding *exclude* (the new fact being
        written). Newest-wins auto-supersede is driven entirely off this --
        no LLM, no critic, deterministic."""
        s = self.store
        res = s.query_facts(predicate=_MEMORY_PREDICATE)  # type: ignore[attr-defined]
        out: list[Any] = []
        for f in res.output:
            if f.subject == exclude:
                continue
            if self._payload_text(f.object, None) != predicate:
                continue
            if not self._is_current(f.subject):
                continue
            wing_name, _room_name = self._resolve_wing_room(f.subject)
            if wing_name != wing:
                continue
            out.append(f.subject)
        return out

    @staticmethod
    def _fact_payload(
        *,
        text: str,
        fact_type: str,
        valid_at: str | None,
        invalid_at: str | None,
        predicate: str | None,
    ) -> bytes:
        """Canonical, content-addressed fact-cell payload (design \u00a72 data
        model): identical normalized text/type/predicate (and valid/invalid
        window) always yields the SAME cell ref."""
        body = {
            "kind": "fact",
            "text": text,
            "fact_type": fact_type,
            "valid_at": valid_at,
            "invalid_at": invalid_at,
            "predicate": predicate,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def fact_add(
        self,
        *,
        text: str,
        fact_type: str,
        source_refs: Sequence[Any],
        wing: str,
        room: str | None = None,
        valid_at: str | None = None,
        invalid_at: str | None = None,
        predicate: str | None = None,
        supersedes: Any | None = None,
        conflicts_with: Any | None = None,
        observed_at: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """Add or reinforce one durable L2 fact (T2.1, D12).

        Validates ``fact_type`` (one of ``_FACT_TYPES``), word count
        (``_FACT_MIN_WORDS``-``_FACT_MAX_WORDS``), redacts *text* and REJECTS
        it if redaction changed anything (facts must never carry secrets),
        and requires every ``source_refs`` entry to be an existing drawer
        (provenance is mandatory -- D12).

        Content-addressed dedup: if the fact cell this call would produce
        already exists and is current, this call only adds any NEW
        ``derived_from`` edges and returns ``deduped=True`` -- no duplicate
        ``recorded_at``/``@memory:current`` assertions.

        ``supersedes`` explicitly retires one prior fact; ``predicate``
        additionally retires (T2.7) every OTHER currently-valid fact in the
        same *wing* asserting the same predicate (newest-wins, no LLM).
        Superseding never deletes: the old fact's ``@memory:current`` is
        invalidated and an ``expired_at`` fact is asserted on it.

        ``conflicts_with`` records an unresolved contradiction as a tension
        cell (reusing amplifier_data.integrity's edge names when
        importable) -- both facts stay current; a person resolves it later.

        All writes for one call land in ONE atomic ``WriteBatch`` when the
        backend supports it (:meth:`_supports_atomic_update`), else
        sequentially (mirrors :meth:`file`).

        Returns ``{"ref", "deduped", "proof_count", "superseded", "tension"}``.
        """
        if fact_type not in _FACT_TYPES:
            raise ValueError(
                f"fact_type must be one of {sorted(_FACT_TYPES)}, got {fact_type!r}"
            )
        normalized_text = " ".join(text.split())
        word_count = len(normalized_text.split()) if normalized_text else 0
        if not (_FACT_MIN_WORDS <= word_count <= _FACT_MAX_WORDS):
            raise ValueError(
                f"fact text must be {_FACT_MIN_WORDS}-{_FACT_MAX_WORDS} words "
                f"(got {word_count})"
            )

        from .redact import redact as _redact

        _scrubbed, redaction_counts = _redact(normalized_text)
        if redaction_counts:
            raise ValueError(
                "fact text appears to contain secret-shaped content and was "
                f"rejected (categories: {sorted(redaction_counts)})"
            )
        if not source_refs:
            raise ValueError(
                "fact_add requires at least one source_ref (provenance is "
                "mandatory, D12)"
            )
        source_list = list(source_refs)
        for src in source_list:
            if self._classify_ref(src) != "drawer":
                raise ValueError(
                    f"source_ref {src!r} is not a known drawer; provenance is mandatory"
                )
        if supersedes is not None and self._classify_ref(supersedes) != "fact":
            raise ValueError(f"supersedes={supersedes!r} is not an existing fact")
        if conflicts_with is not None and self._classify_ref(conflicts_with) != "fact":
            raise ValueError(
                f"conflicts_with={conflicts_with!r} is not an existing fact"
            )

        s = self.store
        payload = self._fact_payload(
            text=normalized_text,
            fact_type=fact_type,
            valid_at=valid_at,
            invalid_at=invalid_at,
            predicate=predicate,
        )
        probe_ref = s.write_cell(payload)  # type: ignore[attr-defined] -- idempotent probe

        if self._is_current(probe_ref):
            existing = set(self._derived_from_refs(probe_ref))
            new_sources = [r for r in source_list if r not in existing]
            for src in new_sources:
                s.assert_fact(probe_ref, _MEMORY_DERIVED_FROM, src)  # type: ignore[attr-defined]
            return {
                "ref": probe_ref,
                "deduped": True,
                "proof_count": len(existing | set(new_sources)),
                "superseded": [],
                "tension": None,
            }

        recorded_at = datetime.now(UTC).isoformat(timespec="seconds")

        to_supersede: list[Any] = []
        if supersedes is not None:
            to_supersede.append(supersedes)
        if predicate is not None:
            for auto in self._current_facts_with_predicate(
                predicate, wing=wing, exclude=probe_ref
            ):
                if auto not in to_supersede:
                    to_supersede.append(auto)

        tension_payload_bytes: bytes | None = None
        if conflicts_with is not None:
            parties = sorted([str(probe_ref), str(conflicts_with)])
            tension_payload_bytes = json.dumps(
                {"kind": "memory_conflict", "parties": parties},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

        atomic = self._supports_atomic_update()
        tension_cell: Any | None = None
        if atomic:
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            cell_ref = b.write_cell(payload)
            b.scope(cell_ref, b.write_cell(f"wing:{wing}".encode()))
            if room is not None:
                b.scope(cell_ref, b.write_cell(f"room:{room}".encode()))
            for src in source_list:
                b.assert_fact(cell_ref, _MEMORY_DERIVED_FROM, src)
            b.assert_fact(
                cell_ref, _RECORDED_AT_PREDICATE, b.write_cell(recorded_at.encode())
            )
            current_marker = b.write_cell(_TRUE_CELL_BYTES)
            b.assert_fact(cell_ref, _MEMORY_CURRENT, current_marker)
            if predicate is not None:
                b.assert_fact(
                    cell_ref, _MEMORY_PREDICATE, b.write_cell(predicate.encode())
                )
            if observed_at is not None:
                b.assert_fact(
                    cell_ref,
                    _OBSERVED_AT_PREDICATE,
                    b.write_cell(observed_at.encode()),
                )
            if embedding is not None:
                from amplifier_data.lenses.vector import EMBEDDING_OF

                vec = list(embedding)
                emb_ref = b.write_cell(struct.pack(f"<{len(vec)}f", *vec))
                b.relate(emb_ref, cell_ref, EMBEDDING_OF)
            for old_ref in to_supersede:
                b.assert_fact(cell_ref, _MEMORY_SUPERSEDES, old_ref)
                b.relate(old_ref, current_marker, INVALIDATE_PREFIX + _MEMORY_CURRENT)
                b.assert_fact(
                    old_ref,
                    _EXPIRED_AT_PREDICATE,
                    b.write_cell(recorded_at.encode()),
                )
            if tension_payload_bytes is not None:
                tension_cell = b.write_cell(tension_payload_bytes)
                for party in (cell_ref, conflicts_with):
                    b.assert_fact(tension_cell, _TENSION_EDGE, party)
                    b.assert_fact(party, _TENSION_IN_TENSION, tension_cell)
                b.assert_fact(cell_ref, _TENSION_CONFLICTS_WITH, conflicts_with)
                b.assert_fact(conflicts_with, _TENSION_CONFLICTS_WITH, cell_ref)
            commit_result = b.commit()
            fact_ref = _resolve_batch_ref(commit_result, cell_ref)
            if tension_cell is not None:
                tension_cell = _resolve_batch_ref(commit_result, tension_cell)
        else:
            cell_ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.scope(cell_ref, s.write_cell(f"wing:{wing}".encode()))  # type: ignore[attr-defined]
            if room is not None:
                s.scope(cell_ref, s.write_cell(f"room:{room}".encode()))  # type: ignore[attr-defined]
            for src in source_list:
                s.assert_fact(cell_ref, _MEMORY_DERIVED_FROM, src)  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                cell_ref, _RECORDED_AT_PREDICATE, s.write_cell(recorded_at.encode())
            )
            current_marker = s.write_cell(_TRUE_CELL_BYTES)  # type: ignore[attr-defined]
            s.assert_fact(cell_ref, _MEMORY_CURRENT, current_marker)  # type: ignore[attr-defined]
            if predicate is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    cell_ref, _MEMORY_PREDICATE, s.write_cell(predicate.encode())
                )
            if observed_at is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    cell_ref,
                    _OBSERVED_AT_PREDICATE,
                    s.write_cell(observed_at.encode()),
                )
            if embedding is not None:
                s.add_embedding(cell_ref, list(embedding))  # type: ignore[attr-defined]
            for old_ref in to_supersede:
                s.assert_fact(cell_ref, _MEMORY_SUPERSEDES, old_ref)  # type: ignore[attr-defined]
                s.invalidate_fact(old_ref, _MEMORY_CURRENT, current_marker)  # type: ignore[attr-defined]
                s.assert_fact(  # type: ignore[attr-defined]
                    old_ref,
                    _EXPIRED_AT_PREDICATE,
                    s.write_cell(recorded_at.encode()),
                )
            if tension_payload_bytes is not None:
                tension_cell = s.write_cell(tension_payload_bytes)  # type: ignore[attr-defined]
                for party in (cell_ref, conflicts_with):
                    s.assert_fact(tension_cell, _TENSION_EDGE, party)  # type: ignore[attr-defined]
                    s.assert_fact(party, _TENSION_IN_TENSION, tension_cell)  # type: ignore[attr-defined]
                s.assert_fact(cell_ref, _TENSION_CONFLICTS_WITH, conflicts_with)  # type: ignore[attr-defined]
                s.assert_fact(conflicts_with, _TENSION_CONFLICTS_WITH, cell_ref)  # type: ignore[attr-defined]
            fact_ref = cell_ref

        return {
            "ref": fact_ref,
            "deduped": False,
            "proof_count": len(set(source_list)),
            "superseded": to_supersede,
            "tension": tension_cell,
        }

    def facts(
        self,
        *,
        query: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        current_only: bool = True,
        k: int = 10,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """List/query L2 facts (T2.1). Ranked by lexical match when *query*
        is given, else newest ``recorded_at`` first. See :meth:`fact_add`."""
        from amplifier_data.lenses._scope import fold_scope

        s = self.store
        scope_ref = None
        if room is not None:
            scope_ref = self._scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._scope_ref("wing", wing)
        fold = self._fold_snapshot()

        scope_index = (
            fold.scope_index() if fold is not None else fold_scope(s.kernel)  # type: ignore[attr-defined]
        )
        universe = (
            set(scope_index.cells_in_scope(scope_ref))
            if scope_ref is not None
            else set(scope_index.membership.keys())
        )
        candidates = [
            ref for ref in universe if self._classify_ref(ref, fold) == "fact"
        ]

        if since is not None or until is not None:
            eligible = self._temporal_eligible(set(candidates), fold, since, until)
            candidates = [r for r in candidates if r in eligible]

        rows: list[dict[str, Any]] = []
        for ref in candidates:
            row = self._fact_row(ref, fold)
            if current_only and not row["current"]:
                continue
            rows.append(row)

        if query:
            from .embedder import lexical_score

            rows.sort(key=lambda r: lexical_score(query, r["text"]), reverse=True)
        else:
            rows.sort(key=lambda r: r["recorded_at"] or "", reverse=True)

        return rows[: max(0, k)]

    def _fact_row(self, ref: Any, fold: _SearchFold | None) -> dict[str, Any]:
        text_raw = self._payload_text(ref, fold)
        try:
            body = json.loads(text_raw)
        except (ValueError, TypeError):
            body = {}
        return {
            "ref": ref,
            "text": body.get("text", ""),
            "fact_type": body.get("fact_type"),
            "predicate": body.get("predicate"),
            "valid_at": body.get("valid_at"),
            "invalid_at": body.get("invalid_at"),
            "proof_count": len(self._derived_from_refs(ref, fold)),
            "derived_from": self._derived_from_refs(ref, fold),
            "recorded_at": self._first_fact_value(ref, _RECORDED_AT_PREDICATE, fold),
            "current": self._is_current(ref, fold),
            "superseded_by": self._superseded_by(ref, fold),
        }

    # ------------------------------------------------------------------
    # P2 -- durable reflection job queue (T2.5/D10)
    # ------------------------------------------------------------------

    def _job_state(self, job_ref: Any) -> str | None:
        return self._first_fact_value(job_ref, _MEMORY_JOB_STATE)

    def reflection_job_add(
        self,
        *,
        span_text: str,
        session_id: str | None,
        trigger: str,
        wing: str,
        room: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Queue one durable reflection job (T2.5/D10): redact *span_text*,
        file it as a conversation drawer, and create a pending job cell
        pointing at it. Idempotent: the same span/session/trigger always
        content-addresses to the SAME job ref, and re-adding an existing job
        leaves its state untouched (no duplicate ``pending`` assertion).

        Returns ``{"job_ref", "span_ref", "state", "redactions"}``.
        """
        from .redact import redact as _redact

        redacted_text, redaction_counts = _redact(span_text)
        s = self.store
        room_resolved = room if room is not None else "conversation"
        filed_at = datetime.now(UTC).isoformat(timespec="seconds")
        atomic = self._supports_atomic_update()

        if atomic:
            b = s.write_batch()  # type: ignore[attr-defined]
            span_ref = b.write_cell(redacted_text.encode("utf-8"))
            b.scope(span_ref, b.write_cell(f"wing:{wing}".encode()))
            b.scope(span_ref, b.write_cell(f"room:{room_resolved}".encode()))
            b.assert_fact(span_ref, "has_source", b.write_cell(b"reflection"))
            b.assert_fact(span_ref, "has_category", b.write_cell(b"conversation"))
            b.assert_fact(
                span_ref, _FILED_AT_PREDICATE, b.write_cell(filed_at.encode())
            )
            if session_id is not None:
                b.assert_fact(
                    span_ref, _IN_SESSION_PREDICATE, b.write_cell(session_id.encode())
                )
            commit_result = b.commit()
            span_ref = _resolve_batch_ref(commit_result, span_ref)
        else:
            span_ref = s.write_cell(redacted_text.encode("utf-8"))  # type: ignore[attr-defined]
            s.scope(span_ref, s.write_cell(f"wing:{wing}".encode()))  # type: ignore[attr-defined]
            s.scope(span_ref, s.write_cell(f"room:{room_resolved}".encode()))  # type: ignore[attr-defined]
            s.assert_fact(span_ref, "has_source", s.write_cell(b"reflection"))  # type: ignore[attr-defined]
            s.assert_fact(span_ref, "has_category", s.write_cell(b"conversation"))  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                span_ref, _FILED_AT_PREDICATE, s.write_cell(filed_at.encode())
            )
            if session_id is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    span_ref,
                    _IN_SESSION_PREDICATE,
                    s.write_cell(session_id.encode()),
                )

        self._file_filing(
            drawer_ref=span_ref,
            wing=wing,
            room=room_resolved,
            source="reflection",
            category="conversation",
            filed_at=filed_at,
            session_id=session_id,
            commit=None,
        )

        job_payload = json.dumps(
            {
                "kind": "reflection_job",
                "span": span_ref,
                "session": session_id,
                "trigger": trigger,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        job_ref = s.write_cell(job_payload)  # type: ignore[attr-defined] -- idempotent probe

        existing_state = self._job_state(job_ref)
        if existing_state is not None:
            return {
                "job_ref": job_ref,
                "span_ref": span_ref,
                "state": existing_state,
                "redactions": redaction_counts,
            }

        if atomic:
            b = s.write_batch()  # type: ignore[attr-defined]
            jref = b.write_cell(job_payload)
            b.scope(jref, b.write_cell(f"wing:{wing}".encode()))
            b.assert_fact(jref, _MEMORY_JOB_STATE, b.write_cell(b"pending"))
            b.assert_fact(jref, _RECORDED_AT_PREDICATE, b.write_cell(filed_at.encode()))
            if observed_at is not None:
                b.assert_fact(
                    jref, _OBSERVED_AT_PREDICATE, b.write_cell(observed_at.encode())
                )
            commit_result = b.commit()
            job_ref = _resolve_batch_ref(commit_result, jref)
        else:
            s.scope(job_ref, s.write_cell(f"wing:{wing}".encode()))  # type: ignore[attr-defined]
            s.assert_fact(job_ref, _MEMORY_JOB_STATE, s.write_cell(b"pending"))  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                job_ref, _RECORDED_AT_PREDICATE, s.write_cell(filed_at.encode())
            )
            if observed_at is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    job_ref,
                    _OBSERVED_AT_PREDICATE,
                    s.write_cell(observed_at.encode()),
                )

        return {
            "job_ref": job_ref,
            "span_ref": span_ref,
            "state": "pending",
            "redactions": redaction_counts,
        }

    def reflection_jobs(
        self, *, state: str = "pending", wing: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Queued reflection jobs in *state* (oldest first). See
        :meth:`reflection_job_add`/:meth:`reflection_job_done`."""
        s = self.store
        res = s.query_facts(predicate=_MEMORY_JOB_STATE)  # type: ignore[attr-defined]
        rows: list[dict[str, Any]] = []
        for f in res.output:
            job_ref = f.subject
            if self._payload_text(f.object, None) != state:
                continue
            if wing is not None:
                job_wing, _room = self._resolve_wing_room(job_ref)
                if job_wing != wing:
                    continue
            try:
                body = json.loads(self._payload_text(job_ref, None))
            except (ValueError, TypeError):
                body = {}
            span_ref = body.get("span")
            span_text = self._payload_text(span_ref, None) if span_ref else ""
            span_wing, span_room = (
                self._resolve_wing_room(span_ref) if span_ref else (None, None)
            )
            rows.append(
                {
                    "job_ref": job_ref,
                    "span_ref": span_ref,
                    "span_text": span_text,
                    "session_id": body.get("session"),
                    "trigger": body.get("trigger"),
                    "wing": span_wing,
                    "room": span_room,
                    "observed_at": self._first_fact_value(
                        job_ref, _OBSERVED_AT_PREDICATE
                    ),
                    "recorded_at": self._first_fact_value(
                        job_ref, _RECORDED_AT_PREDICATE
                    ),
                }
            )
        rows.sort(key=lambda r: r["recorded_at"] or "")
        return rows[: max(0, limit)]

    def reflection_job_done(
        self,
        *,
        job_ref: Any,
        fact_refs: Sequence[Any] = (),
        noop: bool = False,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Close a pending reflection job (T2.5). Raises :class:`ValueError`
        if *job_ref* is not currently pending -- a job is closed exactly
        once."""
        current_state = self._job_state(job_ref)
        if current_state != "pending":
            raise ValueError(
                f"reflection job {job_ref!r} is not pending (state={current_state!r})"
            )
        s = self.store
        pending_marker = s.write_cell(b"pending")  # type: ignore[attr-defined]
        done_marker = s.write_cell(b"done")  # type: ignore[attr-defined]

        if self._supports_atomic_update():
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            b.relate(job_ref, pending_marker, INVALIDATE_PREFIX + _MEMORY_JOB_STATE)
            b.assert_fact(job_ref, _MEMORY_JOB_STATE, done_marker)
            for fref in fact_refs:
                b.assert_fact(job_ref, _MEMORY_JOB_PRODUCED, fref)
            if note is not None:
                b.assert_fact(
                    job_ref,
                    "noop_note" if noop else "note",
                    b.write_cell(note.encode()),
                )
            b.commit()
        else:
            s.invalidate_fact(job_ref, _MEMORY_JOB_STATE, pending_marker)  # type: ignore[attr-defined]
            s.assert_fact(job_ref, _MEMORY_JOB_STATE, done_marker)  # type: ignore[attr-defined]
            for fref in fact_refs:
                s.assert_fact(job_ref, _MEMORY_JOB_PRODUCED, fref)  # type: ignore[attr-defined]
            if note is not None:
                s.assert_fact(  # type: ignore[attr-defined]
                    job_ref,
                    "noop_note" if noop else "note",
                    s.write_cell(note.encode()),
                )

        return {
            "job_ref": job_ref,
            "state": "done",
            "fact_refs": list(fact_refs),
            "noop": noop,
        }

    # ------------------------------------------------------------------
    # P3 -- L3 index (T3.1) and standing questions (T3.3), D19.
    #
    # D19 (index is mechanism-first): an LLM-free deterministic index is
    # ALWAYS available (:meth:`index` computes a derived view on read, no
    # write, when no curated cell exists or it has drifted too far); the
    # curator agent MAY overwrite it with better prose via :meth:`index_set`.
    # L3 text is navigation only -- never cited as evidence (see
    # project-context/GLOSSARY.md's "Index / abstract" entry).
    # ------------------------------------------------------------------

    def _room_members(
        self, room: str, fold: _SearchFold | None
    ) -> tuple[list[Any], list[Any]]:
        """``(current_fact_refs, drawer_refs)`` scoped to *room*, in ONE pass
        over the room's scope membership -- shared by :meth:`index` (via
        :meth:`_index_for_room`) and :meth:`index_set` (freshness count)."""
        from amplifier_data.lenses._scope import fold_scope

        room_scope_ref = self._scope_ref("room", room)
        scope_index = (
            fold.scope_index() if fold is not None else fold_scope(self.store.kernel)  # type: ignore[attr-defined]
        )
        member_refs = scope_index.cells_in_scope(room_scope_ref)
        facts: list[Any] = []
        drawers: list[Any] = []
        for ref in member_refs:
            kind = self._classify_ref(ref, fold)
            if kind == "fact":
                if self._is_current(ref, fold):
                    facts.append(ref)
            elif kind == "drawer":
                drawers.append(ref)
        return facts, drawers

    def _rooms_in_wing(self, wing: str, fold: _SearchFold | None) -> list[str]:
        """Distinct room names among every cell scoped to *wing* (T3.1:
        ``index(wing)`` with no ``room`` enumerates one entry per room)."""
        from amplifier_data.lenses._scope import fold_scope

        wing_scope_ref = self._scope_ref("wing", wing)
        scope_index = (
            fold.scope_index() if fold is not None else fold_scope(self.store.kernel)  # type: ignore[attr-defined]
        )
        member_refs = scope_index.cells_in_scope(wing_scope_ref)
        rooms: set[str] = set()
        for ref in member_refs:
            _wing_name, room_name = self._resolve_wing_room(ref, fold)
            if room_name:
                rooms.add(room_name)
        return sorted(rooms)

    def _current_index_ref(self, room: str, fold: _SearchFold | None) -> Any | None:
        room_scope_ref = self._scope_ref("room", room)
        refs = self._current_fact_refs(room_scope_ref, _MEMORY_CURRENT_INDEX, fold)
        return refs[-1] if refs else None

    def _index_body(self, cell_ref: Any, fold: _SearchFold | None) -> dict[str, Any]:
        text = self._payload_text(cell_ref, fold)
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = {}
        built_from_count = self._first_fact_value(
            cell_ref, _BUILT_FROM_COUNT_PREDICATE, fold
        )
        return {
            "abstract": body.get("abstract", ""),
            "overview": body.get("overview", ""),
            "built_from_count": int(built_from_count)
            if built_from_count is not None
            else 0,
            "built_at": self._first_fact_value(cell_ref, _BUILT_AT_PREDICATE, fold),
        }

    def _derive_index_from_members(
        self,
        room: str,
        fact_refs: list[Any],
        drawer_refs: list[Any],
        fold: _SearchFold | None,
        current_count: int,
    ) -> dict[str, Any]:
        """Compute a navigation-only index for *room* with NO write (D19).

        Abstract/overview are built from the most-supported CURRENT facts
        (``proof_count`` desc, then ``recorded_at`` desc); a room with no
        facts falls back to its latest drawer's first line. Overview then
        appends every drawer's first line (newest first) up to the char cap.
        """
        fact_rows = [self._fact_row(r, fold) for r in fact_refs]
        fact_rows.sort(
            key=lambda r: (r["proof_count"], r["recorded_at"] or ""), reverse=True
        )

        def _first_line(ref: Any) -> str:
            text = self._payload_text(ref, fold)
            return text.splitlines()[0] if text else ""

        ordered_drawers: list[Any] = []
        if drawer_refs:
            times = self.drawer_times(sorted(drawer_refs), _fold=fold)
            ordered_drawers = sorted(
                drawer_refs, key=lambda r: times.get(r) or "", reverse=True
            )

        if fact_rows:
            abstract_source = "; ".join(r["text"] for r in fact_rows)
            overview_parts = [r["text"] for r in fact_rows]
        elif ordered_drawers:
            abstract_source = _first_line(ordered_drawers[0])
            overview_parts = []
        else:
            abstract_source = ""
            overview_parts = []

        overview_parts.extend(_first_line(r) for r in ordered_drawers)

        return {
            "scope": f"room:{room}",
            "abstract": abstract_source[:_INDEX_ABSTRACT_MAX_CHARS],
            "overview": "\n".join(p for p in overview_parts if p)[
                :_INDEX_OVERVIEW_MAX_CHARS
            ],
            "source": "derived",
            "built_from_count": current_count,
            "current_count": current_count,
            "pending_changes": 0,
            "built_at": None,
        }

    def _index_for_room(self, room: str, fold: _SearchFold | None) -> dict[str, Any]:
        fact_refs, drawer_refs = self._room_members(room, fold)
        current_count = len(fact_refs) + len(drawer_refs)
        cur_ref = self._current_index_ref(room, fold)
        if cur_ref is not None:
            body = self._index_body(cur_ref, fold)
            pending = max(0, current_count - body["built_from_count"])
            if pending <= _INDEX_STALE_AFTER_DEFAULT:
                return {
                    "scope": f"room:{room}",
                    "abstract": body["abstract"],
                    "overview": body["overview"],
                    "source": "curated",
                    "built_from_count": body["built_from_count"],
                    "current_count": current_count,
                    "pending_changes": pending,
                    "built_at": body["built_at"],
                }
        return self._derive_index_from_members(
            room, fact_refs, drawer_refs, fold, current_count
        )

    def index(self, *, wing: str, room: str | None = None) -> list[dict[str, Any]]:
        """L3 index read (T3.1, D19): one entry per room in *wing* (or the
        one *room*, when given). ONE :class:`_SearchFold` for the whole
        call regardless of room count -- never a per-room regenerate/re-fold.

        Each entry is either the curated cell (``source:"curated"``) when
        one exists and ``pending_changes`` (facts/drawers filed since it was
        built) is within :data:`_INDEX_STALE_AFTER_DEFAULT`, or a computed
        ``source:"derived"`` view otherwise -- the index is ALWAYS available
        without an LLM (D19: mechanism-first).
        """
        fold = self._fold_snapshot()
        rooms = [room] if room is not None else self._rooms_in_wing(wing, fold)
        return [self._index_for_room(r, fold) for r in rooms]

    def index_set(
        self,
        *,
        scope: str,
        abstract: str,
        overview: str,
        cites: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Curator write path (T3.1): overwrite the current index for *scope*
        (``"room:<name>"``). Validates length caps (rejects over-length,
        never truncates silently), redacts and REJECTS secret-shaped text,
        and requires every *cites* entry to be an existing fact (L3 never
        cites evidence that doesn't exist -- and never IS evidence itself,
        see the module's P3 docstring).

        Supersede-never-delete (D12's pattern, reused here): the room scope
        cell's ``@memory:current_index`` fact is invalidated and reasserted
        to point at the new cell; the old index cell is left untouched.
        """
        if len(abstract) > _INDEX_ABSTRACT_MAX_CHARS:
            raise ValueError(
                f"abstract must be <= {_INDEX_ABSTRACT_MAX_CHARS} chars "
                f"(got {len(abstract)})"
            )
        if len(overview) > _INDEX_OVERVIEW_MAX_CHARS:
            raise ValueError(
                f"overview must be <= {_INDEX_OVERVIEW_MAX_CHARS} chars "
                f"(got {len(overview)})"
            )
        if not scope.startswith("room:"):
            raise ValueError(f"scope must be 'room:<name>' (got {scope!r})")
        room = scope[len("room:") :]

        from .redact import redact as _redact

        _scrub_a, redactions_a = _redact(abstract)
        _scrub_o, redactions_o = _redact(overview)
        if redactions_a or redactions_o:
            raise ValueError(
                "index text appears to contain secret-shaped content and was rejected"
            )

        cite_list = list(cites)
        for c in cite_list:
            if self._classify_ref(c) != "fact":
                raise ValueError(f"cite {c!r} is not an existing fact")

        s = self.store
        room_scope_ref = self._scope_ref("room", room)
        fact_refs, drawer_refs = self._room_members(room, None)
        current_count = len(fact_refs) + len(drawer_refs)
        built_at = datetime.now(UTC).isoformat(timespec="seconds")
        payload = json.dumps(
            {
                "kind": "index",
                "scope": scope,
                "abstract": abstract,
                "overview": overview,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        old_current = self._current_fact_refs(room_scope_ref, _MEMORY_CURRENT_INDEX)

        if self._supports_atomic_update():
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            cell_ref = b.write_cell(payload)
            b.scope(cell_ref, room_scope_ref)
            b.assert_fact(
                cell_ref, _BUILT_AT_PREDICATE, b.write_cell(built_at.encode())
            )
            b.assert_fact(
                cell_ref,
                _BUILT_FROM_COUNT_PREDICATE,
                b.write_cell(str(current_count).encode()),
            )
            for c in cite_list:
                b.assert_fact(cell_ref, _MEMORY_CITES, c)
            for old in old_current:
                b.relate(room_scope_ref, old, INVALIDATE_PREFIX + _MEMORY_CURRENT_INDEX)
            b.assert_fact(room_scope_ref, _MEMORY_CURRENT_INDEX, cell_ref)
            commit_result = b.commit()
            cell_ref = _resolve_batch_ref(commit_result, cell_ref)
        else:
            cell_ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.scope(cell_ref, room_scope_ref)  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                cell_ref, _BUILT_AT_PREDICATE, s.write_cell(built_at.encode())
            )
            s.assert_fact(  # type: ignore[attr-defined]
                cell_ref,
                _BUILT_FROM_COUNT_PREDICATE,
                s.write_cell(str(current_count).encode()),
            )
            for c in cite_list:
                s.assert_fact(cell_ref, _MEMORY_CITES, c)  # type: ignore[attr-defined]
            for old in old_current:
                s.invalidate_fact(room_scope_ref, _MEMORY_CURRENT_INDEX, old)  # type: ignore[attr-defined]
            s.assert_fact(room_scope_ref, _MEMORY_CURRENT_INDEX, cell_ref)  # type: ignore[attr-defined]

        return {
            "ref": cell_ref,
            "built_from_count": current_count,
            "built_at": built_at,
        }

    # -- Standing questions (T3.3) --------------------------------------

    def standing_add(self, *, question: str, wing: str) -> dict[str, Any]:
        """Content-addressed, idempotent standing-question cell, scoped to
        *wing*. Re-adding the same question text is a no-op read (the
        ``write_cell``/``scope`` calls are themselves idempotent)."""
        normalized = " ".join(question.split())
        payload = json.dumps(
            {"kind": "standing_question", "question": normalized},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        s = self.store
        ref = s.write_cell(payload)  # type: ignore[attr-defined]
        s.scope(ref, self._scope_ref("wing", wing))  # type: ignore[attr-defined]
        return {"ref": ref}

    def standing_answer(
        self,
        *,
        question_ref: Any,
        answer: str,
        cites: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Supersede the current answer for *question_ref* (D12's pattern:
        never deletes -- invalidates the question's ``@memory:current_answer``
        fact and reasserts it at the new answer cell). *cites* must each be
        an existing fact; the answer text is redacted and REJECTED if it
        appears to contain secret-shaped content."""
        if self._classify_ref(question_ref) != "standing_question":
            raise ValueError(
                f"question_ref {question_ref!r} is not a known standing question"
            )

        from .redact import redact as _redact

        _scrub, redactions = _redact(answer)
        if redactions:
            raise ValueError(
                "standing answer appears to contain secret-shaped content and "
                "was rejected"
            )

        cite_list = list(cites)
        for c in cite_list:
            if self._classify_ref(c) != "fact":
                raise ValueError(f"cite {c!r} is not an existing fact")

        s = self.store
        answered_at = datetime.now(UTC).isoformat(timespec="seconds")
        payload = json.dumps(
            {"kind": "standing_answer", "question": question_ref, "answer": answer},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        old_current = self._current_fact_refs(question_ref, _MEMORY_CURRENT_ANSWER)

        if self._supports_atomic_update():
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            ans_ref = b.write_cell(payload)
            b.assert_fact(
                ans_ref, _ANSWERED_AT_PREDICATE, b.write_cell(answered_at.encode())
            )
            for c in cite_list:
                b.assert_fact(ans_ref, _MEMORY_CITES, c)
            for old in old_current:
                b.relate(question_ref, old, INVALIDATE_PREFIX + _MEMORY_CURRENT_ANSWER)
            b.assert_fact(question_ref, _MEMORY_CURRENT_ANSWER, ans_ref)
            commit_result = b.commit()
            ans_ref = _resolve_batch_ref(commit_result, ans_ref)
        else:
            ans_ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.assert_fact(  # type: ignore[attr-defined]
                ans_ref, _ANSWERED_AT_PREDICATE, s.write_cell(answered_at.encode())
            )
            for c in cite_list:
                s.assert_fact(ans_ref, _MEMORY_CITES, c)  # type: ignore[attr-defined]
            for old in old_current:
                s.invalidate_fact(question_ref, _MEMORY_CURRENT_ANSWER, old)  # type: ignore[attr-defined]
            s.assert_fact(question_ref, _MEMORY_CURRENT_ANSWER, ans_ref)  # type: ignore[attr-defined]

        return {"ref": ans_ref, "answered_at": answered_at}

    def standing(self, *, wing: str) -> list[dict[str, Any]]:
        """Every standing question scoped to *wing* (T3.3), ONE fold for the
        whole call. ``stale`` iff there is no answer yet, or any cited fact
        is no longer current (retraction check) -- L3 navigation text is
        never trusted past the evidence it points at."""
        from amplifier_data.lenses._scope import fold_scope

        fold = self._fold_snapshot()
        wing_scope_ref = self._scope_ref("wing", wing)
        scope_index = (
            fold.scope_index() if fold is not None else fold_scope(self.store.kernel)  # type: ignore[attr-defined]
        )
        member_refs = scope_index.cells_in_scope(wing_scope_ref)

        rows: list[dict[str, Any]] = []
        for ref in member_refs:
            if self._classify_ref(ref, fold) != "standing_question":
                continue
            try:
                body = json.loads(self._payload_text(ref, fold))
            except (ValueError, TypeError):
                body = {}
            question_text = body.get("question", "")

            answer_refs = self._current_fact_refs(ref, _MEMORY_CURRENT_ANSWER, fold)
            answer_ref = answer_refs[-1] if answer_refs else None

            answer_text: str | None = None
            answered_at: str | None = None
            cites: list[Any] = []
            stale_reasons: list[Any] = []
            if answer_ref is not None:
                try:
                    answer_body = json.loads(self._payload_text(answer_ref, fold))
                except (ValueError, TypeError):
                    answer_body = {}
                answer_text = answer_body.get("answer")
                answered_at = self._first_fact_value(
                    answer_ref, _ANSWERED_AT_PREDICATE, fold
                )
                cites = self._current_fact_refs(answer_ref, _MEMORY_CITES, fold)
                stale_reasons = [c for c in cites if not self._is_current(c, fold)]
                stale = bool(stale_reasons)
            else:
                stale = True

            rows.append(
                {
                    "question_ref": ref,
                    "question": question_text,
                    "answer": answer_text,
                    "answered_at": answered_at,
                    "cites": cites,
                    "stale": stale,
                    "stale_reasons": stale_reasons,
                }
            )
        return rows
