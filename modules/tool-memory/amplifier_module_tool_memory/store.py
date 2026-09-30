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
import os
import re
import struct
import threading
import time
import weakref
from collections import OrderedDict
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

#: T6.4 (D29) capability check, not a hard dependency: the cross-encoder
#: reranker lives in ``rerank.py`` (owned by a concurrent builder). When it
#: is not present/importable, ``rerank`` search requests simply fall back to
#: RRF order with a ``rerank_skipped`` reason on every hit -- never a hard
#: failure.
try:
    from amplifier_module_tool_memory.rerank import get_reranker
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    get_reranker = None  # type: ignore[assignment]

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

#: T6.2 (D29): a passage cell -> its parent drawer. Scoped identically to
#: the drawer (same wing/room edges); NOT a provenance edge (that's
#: ``_MEMORY_DERIVED_FROM``) -- a passage is a derived VIEW of its drawer,
#: not a fact built FROM it.
_MEMORY_PASSAGE_OF = "@memory:passage_of"

#: T6.5 (D29): records which embedder produced a ref's current vector(s),
#: so a model swap can tell stale vectors from current ones (search excludes
#: a mismatch from the semantic arm; a maintenance sweep requeues it for
#: re-embedding). Plain (non-reserved-prefix) fact predicate -- mirrors
#: ``has_source``/``has_category`` rather than the ``@memory:`` edges above,
#: since it is regular queryable KG-shaped metadata, not internal wiring.
_EMBEDDED_WITH_PREDICATE = "embedded_with"

#: T6.2 (D29) passage-splitting defaults (design \u00a76 P6 plan). A drawer's
#: content at or above ``passage_min_chars`` is split into overlapping
#: passages targeting ``passage_chars`` with ``passage_overlap`` chars of
#: back-overlap between consecutive passages -- see
#: :func:`_split_into_passages`. ``max_passages_per_drawer`` bounds how many
#: of one drawer's passages may appear in a single passage-granularity
#: search result (collapse step in :meth:`NativeMemoryStore.search`).
DEFAULT_PASSAGE_MIN_CHARS = 1200
DEFAULT_PASSAGE_CHARS = 900
DEFAULT_PASSAGE_OVERLAP = 150
DEFAULT_MAX_PASSAGES_PER_DRAWER = 2

#: T6.4/D29 P6 rerank defaults (measured 2026-09-28: 30 pairs x 900 chars
#: p95 160ms blows the 150ms search budget; 15-20 pairs or <=800 chars each
#: land around 100ms). Rerank mode itself stays "off" by default (unchanged)
#: -- these are only the top-N/max-chars values applied WHEN a caller (or
#: config) turns reranking on, so opting in gets a budget-safe shape without
#: every caller having to know the measurement.
DEFAULT_RERANK_TOP_N = 20
DEFAULT_RERANK_MAX_CHARS = 800

#: P8/D34 ("rank on passages, read on neighborhoods") expansion defaults:
#: how many passages either side of a hit :meth:`NativeMemoryStore.search`'s
#: ``expand="neighbors"`` pulls in, and the total char budget (~10k tokens
#: at a 4-chars/token estimate) spent expanding hits, in rank order, before
#: later hits keep only their own passage.
DEFAULT_EXPAND_NEIGHBORS = 1
DEFAULT_EXPAND_CHAR_BUDGET = 40000

#: Natural passage-split boundary lines (T6.2, design \u00a74): blank lines,
#: markdown headings, and conversational turn markers (``user:``,
#: ``assistant:``, ``[role]``-style brackets). Matched against a single
#: line (including its trailing newline, if any).
_PASSAGE_BOUNDARY_RE = re.compile(
    r"^[ \t]*($|#{1,6}[ \t]|(user|assistant|system|human|ai)\s*:|\[[^\]\n]+\][ \t]*$)",
    re.IGNORECASE,
)


def _is_passage_boundary_line(line: str) -> bool:
    """Whether *line* is a natural passage-split boundary (see
    :data:`_PASSAGE_BOUNDARY_RE`)."""
    return bool(_PASSAGE_BOUNDARY_RE.match(line))


def _bounded_lines(text: str, max_chars: int) -> list[str]:
    """``text.splitlines(keepends=True)``, except that a line longer than
    *max_chars* is cut into consecutive segments of at most *max_chars*,
    each ending just after the last whitespace in its window (a hard cut
    only when the window has none). The segments concatenate back to the
    original text exactly, so offsets stay valid; deterministic.

    Without this, a drawer that is one long line (minified JSON, a
    serialized transcript, a long log line) became ONE passage the size of
    the whole drawer -- the passage index silently did nothing for it
    (found by the T6.6 benchmark: every LongMemEval session is one line).
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars > 0:
            window = line[:max_chars]
            cut = max(window.rfind(" "), window.rfind("\t"))
            cut = cut + 1 if cut > 0 else max_chars
            out.append(line[:cut])
            line = line[cut:]
        if line:
            out.append(line)
    return out


def _split_into_passages(
    text: str,
    *,
    passage_chars: int = DEFAULT_PASSAGE_CHARS,
    overlap: int = DEFAULT_PASSAGE_OVERLAP,
) -> list[tuple[int, int, str]]:
    """Deterministically split *text* into overlapping ``(start, end, text)``
    passages (T6.2, design \u00a74), splitting ONLY at line boundaries (never
    mid-line).

    Grows each passage line-by-line until it reaches *passage_chars*, then
    looks a short distance ahead (up to 5 more lines) for a natural
    boundary line (:func:`_is_passage_boundary_line`) to actually cut
    after; a long unbroken block with no boundary in that window is cut at
    the line where the target size was first reached -- still a full-line
    boundary, just not a "natural" one. Each following passage restarts
    *overlap* chars before the previous cut, backed up to the nearest line
    start, so no passage boundary can strand a fact mid-thought. Pure and
    deterministic: identical *text* always yields identical output, which
    is what makes the resulting passage cells idempotent under content
    addressing (re-filing the same drawer content produces the SAME
    passage refs).
    """
    if not text:
        return []
    # Over-long lines are cut into segments no longer than the overlap, so
    # passage size and overlap stay near their targets on unbroken text.
    lines = _bounded_lines(text, max(1, min(passage_chars, overlap or passage_chars)))
    if not lines:
        return []
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    total = pos
    n = len(lines)

    def _line_end(idx: int) -> int:
        return offsets[idx] + len(lines[idx])

    passages: list[tuple[int, int, str]] = []
    start_line = 0
    while start_line < n:
        start = offsets[start_line]
        j = start_line
        while j < n and _line_end(j) - start < passage_chars:
            j += 1
        if j >= n:
            j = n - 1
        else:
            lookahead_limit = min(n - 1, j + 5)
            k = j
            while k < lookahead_limit and not _is_passage_boundary_line(lines[k]):
                k += 1
            # No natural boundary in the window: cut where the target size
            # was first reached (as documented), not at the window's end.
            if _is_passage_boundary_line(lines[k]):
                j = k
        end = _line_end(j)
        passages.append((start, end, text[start:end]))
        if end >= total:
            break
        back_to = end - overlap
        k = j
        while k > start_line and offsets[k] > back_to:
            k -= 1
        start_line = max(k, start_line + 1)
    return passages


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


class _CellRefCache:
    """Incremental ``SeqPos-index -> cell_ref`` memo for an append-only log.

    ``CellWriteEvent.cell_ref()`` is an uncached sha256 over canonical JSON;
    recomputing it for every event on every read was the single largest cost
    of a search (~220k hashes per call on a ~400k-event store, ~1.5 s). A
    ref is a pure function of an immutable event, and the log is append-only,
    so the refs for the prefix already seen never change: only the newly
    appended tail is hashed. The prefix is re-validated on every call (length
    and the SeqPos of the last memoized event); any mismatch -- e.g. a
    compacted/rewritten log -- drops the memo and rebuilds from scratch.
    Thread-safe (the daemon serves reads concurrently).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refs: list[Any] = []
        self._last_pos: Any = None

    def refs_for(self, events: Any) -> list[Any]:
        from amplifier_data.models import CellWriteEvent

        with self._lock:
            n = len(self._refs)
            if n > len(events) or (n and events[n - 1][0] != self._last_pos):
                self._refs = []
                n = 0
            if len(events) > n:
                extend = self._refs.append
                for _pos, ev in events[n:]:
                    extend(ev.cell_ref() if isinstance(ev, CellWriteEvent) else None)
                self._last_pos = events[-1][0]
            return self._refs[: len(events)]


class _FoldView:
    """A read-only slice of a :class:`_SearchFold`'s event list.

    Exposes the same ``all_events()`` / ``payloads`` surface, so the
    substrate's own lenses run their exact logic over just the events that
    can affect the answer (see :meth:`_SearchFold.for_subject` and
    :meth:`_SearchFold.vector_view`).
    """

    def __init__(self, events: Any, payloads: dict[Any, bytes]) -> None:
        self._events = events
        self.payloads = payloads

    def all_events(self) -> Any:
        return self._events


class _SearchFold:
    """ONE materialized ``kernel.all_events()`` pass, shared across every lens
    read inside a single :meth:`NativeMemoryStore.search` call.

    Perf seam (perf/search-no-regenerate): the old search path called
    ``store.regenerate(ref)`` per candidate/hit, and each store-surface lens
    read (``query_vector``/``query_facts``/``graph_neighbors``) re-materialized
    the whole event log -- O(reads x log) per query (~12s on a ~38k-event
    durable store). This class pays for the materialization ONCE and exposes
    the only kernel surface the substrate's pure-fold lenses consume
    (``all_events()``), so ``VectorLens``/``TemporalLens``/``GraphLens``/
    ``fold_scope`` run their EXACT own logic over one cached event list --
    reuse of amplifier-data primitives, not a reimplementation.

    It also carries the ``ref -> payload`` join for every ``CellWriteEvent``
    seen in that pass (the same join ``VectorLens.project`` performs), so
    per-hit payload reads need no ``regenerate`` full-log re-fold. Content
    addressing makes the join exact: a ref defined by a ``CellWriteEvent`` is
    the content address of ``(payload, interpreters)``, so every defining
    event for that ref carries an identical payload -- precisely what
    ``regenerate(ref).payload`` returns (replay.py folds to the last defining
    payload). Refs defined some other way (interpreter/index cells) are
    absent from the join and fall back to ``regenerate``.

    perf/startup-latency + perf/incremental-fold: the per-event refs come
    from the store's :class:`_CellRefCache` (only new events are hashed),
    and this fold itself may be built by EXTENDING a *prior* fold (an
    append-only continuation, see :meth:`_is_prefix_of`) instead of
    rescanning the whole log -- the payload join, the per-subject/vector
    indexes, the numpy vector matrix (:meth:`vector_matrix`), AND (scale
    fix / D26) the scope-membership, current-facts triple history and
    filing-cell attribution below are all extended from only the newly
    appended tail on a store dominated by continuous writes.

    Reads the kernel directly -- no AccessEvents (D4 read-vs-fold boundary).
    Lenses stay pure (D1) and every read sees a current view of the log: a
    short-lived burst-read snapshot is reused only by
    :meth:`NativeMemoryStore._fold_snapshot` while NO event has been
    appended since it was taken (tracked by a kernel observer), and only
    for a few seconds, so a burst of reads (the session briefing's search +
    KG + diary, then the first prompt's interject search) pays for one log
    materialization instead of one each.

    T7.1 (D32): the store's OWN persistent extension base
    (``NativeMemoryStore._prior_fold``) is a SEPARATE, longer-lived thing
    from that short burst-read snapshot -- it is never idle-expired, so a
    search arriving after any idle gap (5s, 5 minutes, a day) still only
    EXTENDS this fold from wherever it last left off, rather than
    rebuilding every index in this class from scratch. Before this fix,
    idle expiry dropped the extension base too, so the very next search
    after an idle gap rebuilt the whole thing -- the measured defect (D31):
    384-460s on a 1 GB store after a 6s idle gap, because a from-scratch
    build (``prior=None``) walks every event in the log to rebuild the
    payload join, per-subject index, vector matrix, scope membership,
    current-facts history and filing attribution, instead of processing
    only the newly appended tail.
    """

    def __init__(
        self,
        kernel: Any,
        ref_cache: _CellRefCache | None = None,
        *,
        prior: _SearchFold | None = None,
    ) -> None:
        """Build over *kernel*, or, when *prior* is a valid append-only
        ancestor of the resulting event list (perf/incremental-fold),
        EXTEND its already-computed payload join / per-subject index /
        embedding index / scope-and-facts attribution index with only the
        newly appended tail instead of rescanning the whole log for each of
        them.

        ``kernel.all_events()`` itself (the Rust-to-Python event
        marshaling) has no cheaper incremental form in amplifier-data's
        current API, so that one call is paid unconditionally, exactly as
        before. Everything this class itself does with the result is what
        incremental extension actually saves.
        """
        from amplifier_data.models import CellWriteEvent

        self._events: Any = kernel.all_events()
        if ref_cache is not None:
            refs = ref_cache.refs_for(self._events)
        else:
            refs = [
                ev.cell_ref() if isinstance(ev, CellWriteEvent) else None
                for _pos, ev in self._events
            ]
        self.refs: list[Any] = refs

        extend_from = (
            prior if prior is not None and prior._is_prefix_of(self._events) else None
        )
        start = len(extend_from._events) if extend_from is not None else 0
        new_tail = list(zip(self._events[start:], refs[start:]))

        if extend_from is not None:
            payloads = dict(extend_from.payloads)
        else:
            payloads = {}
        for (_pos, ev), ref in new_tail:
            if ref is not None:
                payloads[ref] = ev.payload
        self.payloads: dict[Any, bytes] = payloads

        new_tail_events = [item for item, _ref in new_tail]
        self._by_subject: dict[Any, list[Any]] | None = (
            extend_from._extend_by_subject(new_tail_events)
            if extend_from is not None
            else None
        )
        self._vector_view: _FoldView | None = None
        self._embedding_edges_cache: list[tuple[str, str]] | None = (
            extend_from._extend_embedding_edges(new_tail_events)
            if extend_from is not None
            else None
        )
        # Incrementally-extendable (target_refs, float64 matrix) cache for
        # the numpy-accelerated vector scorer (perf/incremental-fold) --
        # see :meth:`vector_matrix`.
        self._matrix_state: tuple[list[str], Any, int] | None = (
            extend_from._matrix_state if extend_from is not None else None
        )
        self._vector_matrix_computed = False
        self._vector_matrix: tuple[list[str], Any] | None = None
        # T6.1 (D29): per-scope-ref (target_refs, float64 matrix, edge_count)
        # partitions, keyed by scope_ref (wing OR room ref) -- see
        # :meth:`scoped_vector_matrix`. Shallow-copied from *prior* (each
        # entry is replaced wholesale, never mutated in place) so this
        # fold's extensions never affect an earlier fold sharing the dict.
        self._scope_matrix_states: dict[Any, tuple[list[str], Any, int]] = (
            dict(extend_from._scope_matrix_states) if extend_from is not None else {}
        )

        # T0.2 / scale-defect-1 fix (D26 -- cross-request incrementality):
        # filed_at earliest-wins, scope membership, currently-valid-facts
        # triple history and filing-cell attribution, extended from *prior*
        # exactly like the caches above instead of re-walked on every fold.
        base_filed_at = extend_from.filed_at if extend_from is not None else {}
        self.filed_at: dict[Any, str] = self._merge_filed_at(
            base_filed_at, new_tail_events, self.payloads
        )

        if extend_from is not None:
            base_scope = extend_from.scope_membership
            base_scope_reverse = extend_from.scope_reverse
            base_history = extend_from._triple_history
            base_sp = extend_from._by_subject_predicate
            base_po = extend_from._by_predicate_object
            base_filed_as = extend_from._filed_as_edges
        else:
            (
                base_scope,
                base_scope_reverse,
                base_history,
                base_sp,
                base_po,
                base_filed_as,
            ) = (
                {},
                {},
                {},
                {},
                {},
                [],
            )
        (
            self.scope_membership,
            self.scope_reverse,
            self._triple_history,
            self._by_subject_predicate,
            self._by_predicate_object,
            self._filed_as_edges,
        ) = self._merge_triples(
            base_scope,
            base_scope_reverse,
            base_history,
            base_sp,
            base_po,
            base_filed_as,
            new_tail_events,
        )

        filings: dict[Any, list[tuple[Any, dict[str, Any]]]] = {}
        for drawer_ref, filing_ref in self._filed_as_edges:
            raw = self.payloads.get(filing_ref)
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

    def _is_prefix_of(self, new_events: Any) -> bool:
        """Whether *new_events* is this fold's own event list with only
        new events appended at the tail (append-only continuation)."""
        n = len(self._events)
        if n == 0:
            return True
        if len(new_events) < n:
            return False
        return bool(new_events[n - 1][0] == self._events[-1][0])

    def apply_tail_in_place(
        self, new_tail: list[tuple[Any, Any]], payload_of: dict[Any, bytes]
    ) -> None:
        """T7.1-real (D32) write-through: mutate THIS fold's own indexes
        directly with *new_tail* -- the events one write call just
        committed (real kernel SeqPos + the actual staged ``Event``
        objects, zipped from a ``WriteBatch``'s ``commit()`` result) --
        with NO kernel read at all.

        Unlike the constructor's ``prior=`` extension (which builds a
        brand-new fold by copying every base dict/list once per call --
        fine for one call per SEARCH, catastrophic for one call per
        WRITE), this method APPENDS/mutates in place: cost is
        O(len(new_tail)), never O(store), so it is safe to call on every
        single write without regressing ingest throughput.

        Concurrency: callers (``NativeMemoryStore._apply_write_through``)
        hold ``NativeMemoryStore._index_lock`` for the duration, because
        ``search()`` runs WITHOUT the daemon's write lock (by design, for
        read latency) and could otherwise observe a dict mutated
        mid-iteration. Every mutation here is either an append (safe to
        race with a concurrent reader that only ``.get()``s) or a
        ``setdefault`` inserting a new key (the one operation that CAN
        corrupt a concurrent ``dict.items()`` iteration elsewhere --
        which is exactly why the lock exists).
        """
        from amplifier_data.lenses._scope import SCOPED_TO
        from amplifier_data.lenses.temporal import INVALIDATE_PREFIX
        from amplifier_data.lenses.vector import EMBEDDING_OF
        from amplifier_data.models import CellWriteEvent, RelationshipEvent

        self.payloads.update(payload_of)
        for pos, ev in new_tail:
            self._events.append((pos, ev))
            self.refs.append(ev.cell_ref() if isinstance(ev, CellWriteEvent) else None)
            if self._by_subject is not None and isinstance(ev, RelationshipEvent):
                self._by_subject.setdefault(ev.from_ref, []).append((pos, ev))
            if not isinstance(ev, RelationshipEvent):
                continue
            etype = ev.type
            if etype == SCOPED_TO:
                self.scope_membership.setdefault(ev.from_ref, set()).add(ev.to_ref)
                self.scope_reverse.setdefault(ev.to_ref, set()).add(ev.from_ref)
                continue
            if etype == EMBEDDING_OF:
                if self._embedding_edges_cache is not None:
                    self._embedding_edges_cache.append((ev.from_ref, ev.to_ref))
                continue
            if etype == _MEMORY_FILED_AS:
                self._filed_as_edges.append((ev.from_ref, ev.to_ref))
                raw = self.payloads.get(ev.to_ref)
                if raw is not None:
                    try:
                        body = json.loads(raw.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        body = None
                    if isinstance(body, dict) and body.get("kind") == "filing":
                        self.filings.setdefault(ev.from_ref, []).append(
                            (ev.to_ref, body)
                        )
            if etype.startswith(INVALIDATE_PREFIX):
                predicate = etype[len(INVALIDATE_PREFIX) :]
                op = "invalidate"
            else:
                predicate = etype
                op = "assert"
            self._triple_history.setdefault(
                (ev.from_ref, predicate, ev.to_ref), []
            ).append((pos, op))
            sp_objs = self._by_subject_predicate.setdefault(
                (ev.from_ref, predicate), []
            )
            if ev.to_ref not in sp_objs:
                sp_objs.append(ev.to_ref)
            po_subs = self._by_predicate_object.setdefault((predicate, ev.to_ref), [])
            if ev.from_ref not in po_subs:
                po_subs.append(ev.from_ref)
            if predicate == _FILED_AT_PREDICATE:
                raw = self.payloads.get(ev.to_ref)
                if raw is not None:
                    value = raw.decode("utf-8", errors="replace")
                    existing = self.filed_at.get(ev.from_ref)
                    if existing is None or value < existing:
                        self.filed_at[ev.from_ref] = value
        # The cached (refs, matrix) TUPLE may now be stale, but the
        # incrementally-extendable state it was built from
        # (`_matrix_state`/`_scope_matrix_states`) is NOT invalidated --
        # the next `vector_matrix()`/`scoped_vector_matrix()` call simply
        # re-checks `embedding_edges()` (now current, appended above) and
        # extends from wherever it left off, exactly like the constructor
        # path already does.
        self._vector_matrix_computed = False
        self._vector_view = None

    def _extend_by_subject(
        self, new_tail_events: list[Any]
    ) -> dict[Any, list[Any]] | None:
        """Extend this fold's ``_by_subject`` index with *new_tail_events*,
        or ``None`` if it was never built (stays lazy)."""
        if self._by_subject is None:
            return None
        from amplifier_data.models import RelationshipEvent

        by: dict[Any, list[Any]] = {k: list(v) for k, v in self._by_subject.items()}
        for item in new_tail_events:
            ev = item[1]
            if isinstance(ev, RelationshipEvent):
                by.setdefault(ev.from_ref, []).append(item)
        return by

    def _extend_embedding_edges(
        self, new_tail_events: list[Any]
    ) -> list[tuple[str, str]] | None:
        """Extend this fold's cached ``embedding_of`` edge list with
        *new_tail_events*, or ``None`` if it was never built (stays lazy)."""
        if self._embedding_edges_cache is None:
            return None
        from amplifier_data.lenses.vector import EMBEDDING_OF
        from amplifier_data.models import RelationshipEvent

        edges = list(self._embedding_edges_cache)
        for _pos, ev in new_tail_events:
            if isinstance(ev, RelationshipEvent) and ev.type == EMBEDDING_OF:
                edges.append((ev.from_ref, ev.to_ref))
        return edges

    @staticmethod
    def _merge_filed_at(
        base: dict[Any, str], new_tail_events: list[Any], payloads: dict[Any, bytes]
    ) -> dict[Any, str]:
        """Earliest-``filed_at``-per-ref, extended from *base* over only
        *new_tail_events* (perf/incremental-fold; T0.2's original full-log
        pass, made incremental). ``filed_at`` is never invalidated
        (append-only provenance); "first-seen" is the lexicographically
        (== chronologically, ISO-8601) smallest value. Kept as the LEGACY
        (pre-filing) fallback -- see ``filings``, which is scope-aware and
        preferred whenever it exists."""
        from amplifier_data.models import RelationshipEvent

        filed_at = dict(base)
        for _pos, ev in new_tail_events:
            if isinstance(ev, RelationshipEvent) and ev.type == _FILED_AT_PREDICATE:
                raw = payloads.get(ev.to_ref)
                if raw is None:
                    continue  # defensive: object cell not seen in this fold
                value = raw.decode("utf-8", errors="replace")
                existing = filed_at.get(ev.from_ref)
                if existing is None or value < existing:
                    filed_at[ev.from_ref] = value
        return filed_at

    @staticmethod
    def _merge_triples(
        base_scope: dict[Any, set[Any]],
        base_scope_reverse: dict[Any, set[Any]],
        base_history: dict[tuple[Any, str, Any], list[tuple[Any, str]]],
        base_sp: dict[tuple[Any, str], list[Any]],
        base_po: dict[tuple[str, Any], list[Any]],
        base_filed_as: list[tuple[Any, Any]],
        new_tail_events: list[Any],
    ) -> tuple[
        dict[Any, set[Any]],
        dict[Any, set[Any]],
        dict[tuple[Any, str, Any], list[tuple[Any, str]]],
        dict[tuple[Any, str], list[Any]],
        dict[tuple[str, Any], list[Any]],
        list[tuple[Any, Any]],
    ]:
        """Scale fix (perf/attribution-and-fold-once, made incremental for
        D26): extend, from the base_* args, everything a per-hit metadata lookup
        used to re-fold the log for:
          - scope_membership: ref -> {scope_ref, ...}, the exact data
            `fold_scope`/`GraphLens.neighbors(rel_type="scoped_to")` used
            to recompute from scratch on every call.
          - triple_history / by_subject_predicate / by_predicate_object:
            mirrors TemporalLens's own fold exactly (assert/invalidate per
            (subject, predicate, object) triple, SeqPos-ordered), so
            `current_objects`/`objects_pointing_to` reproduce
            TemporalLens.current_facts() without a second event-log walk.
          - filed_as edges: drawer ref -> filing ref, resolved into
            `filings` by the caller (needs the completed payload join).
        Only *new_tail_events* is scanned; the base_* args already reflect
        every earlier event (mirrors :meth:`_extend_by_subject`'s
        copy-then-mutate style).
        """
        from amplifier_data.lenses._scope import SCOPED_TO
        from amplifier_data.lenses.temporal import INVALIDATE_PREFIX
        from amplifier_data.lenses.vector import EMBEDDING_OF
        from amplifier_data.models import RelationshipEvent

        scope_membership: dict[Any, set[Any]] = {
            k: set(v) for k, v in base_scope.items()
        }
        scope_reverse: dict[Any, set[Any]] = {
            k: set(v) for k, v in base_scope_reverse.items()
        }
        triple_history: dict[tuple[Any, str, Any], list[tuple[Any, str]]] = {
            k: list(v) for k, v in base_history.items()
        }
        by_subject_predicate: dict[tuple[Any, str], list[Any]] = {
            k: list(v) for k, v in base_sp.items()
        }
        by_predicate_object: dict[tuple[str, Any], list[Any]] = {
            k: list(v) for k, v in base_po.items()
        }
        filed_as_edges: list[tuple[Any, Any]] = list(base_filed_as)
        for seq_pos, ev in new_tail_events:
            if not isinstance(ev, RelationshipEvent):
                continue
            etype = ev.type
            if etype == SCOPED_TO:
                scope_membership.setdefault(ev.from_ref, set()).add(ev.to_ref)
                scope_reverse.setdefault(ev.to_ref, set()).add(ev.from_ref)
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
        return (
            scope_membership,
            scope_reverse,
            triple_history,
            by_subject_predicate,
            by_predicate_object,
            filed_as_edges,
        )

    def all_events(self) -> Any:
        """The read accessor the fold lenses use (mirrors ``StorageKernel``)."""
        return self._events

    def for_subject(self, ref: Any) -> _FoldView:
        """Only the ``RelationshipEvent``s whose ``from_ref`` is *ref*, in log order."""
        if self._by_subject is None:
            from amplifier_data.models import RelationshipEvent

            by: dict[Any, list[Any]] = {}
            for item in self._events:
                ev = item[1]
                if isinstance(ev, RelationshipEvent):
                    by.setdefault(ev.from_ref, []).append(item)
            self._by_subject = by
        return _FoldView(self._by_subject.get(ref, []), self.payloads)

    def vector_view(self) -> _FoldView:
        """The events ``VectorLens.query`` can depend on, in log order:
        ``embedding_of`` and ``scoped_to`` edges plus the embedding cells
        those edges point from."""
        if self._vector_view is None:
            from amplifier_data.lenses._scope import SCOPED_TO
            from amplifier_data.lenses.vector import EMBEDDING_OF
            from amplifier_data.models import RelationshipEvent

            emb_refs = {
                ev.from_ref
                for _pos, ev in self._events
                if isinstance(ev, RelationshipEvent) and ev.type == EMBEDDING_OF
            }
            keep: list[Any] = []
            for item, ref in zip(self._events, self.refs):
                ev = item[1]
                if ref is not None:
                    if ref in emb_refs:
                        keep.append(item)
                elif isinstance(ev, RelationshipEvent) and ev.type in (
                    EMBEDDING_OF,
                    SCOPED_TO,
                ):
                    keep.append(item)
            self._vector_view = _FoldView(keep, self.payloads)
        return self._vector_view

    def embedding_edges(self) -> list[tuple[str, str]]:
        """``[(emb_ref, target_ref)]`` for every ``embedding_of`` edge in
        this fold, in log order. Cached; extended incrementally when built
        via a *prior* fold (see :meth:`_extend_embedding_edges`)."""
        if self._embedding_edges_cache is None:
            from amplifier_data.lenses.vector import EMBEDDING_OF
            from amplifier_data.models import RelationshipEvent

            self._embedding_edges_cache = [
                (ev.from_ref, ev.to_ref)
                for _pos, ev in self._events
                if isinstance(ev, RelationshipEvent) and ev.type == EMBEDDING_OF
            ]
        return self._embedding_edges_cache

    def vector_matrix(self) -> tuple[list[str], Any] | None:
        """``(target_refs, matrix)`` for the numpy-accelerated vector
        scorer (perf/incremental-fold), or ``None`` when numpy is
        unavailable or embeddings have inconsistent dimensions (the caller
        falls back to the pure-Python ``VectorLens`` path in either case).
        """
        if self._vector_matrix_computed:
            return self._vector_matrix
        self._vector_matrix_computed = True
        try:
            import numpy as np
        except ImportError:
            self._vector_matrix = None
            return None

        edges = self.embedding_edges()
        prior_state = self._matrix_state
        if prior_state is not None and prior_state[2] <= len(edges):
            prior_refs, prior_mat, prior_edge_count = prior_state
            new_edges = edges[prior_edge_count:]
        else:
            prior_refs, prior_mat, prior_edge_count = [], None, 0
            new_edges = edges

        new_refs: list[str] = []
        new_rows: list[tuple[float, ...]] = []
        dim = (
            prior_mat.shape[1] if prior_mat is not None and prior_mat.shape[0] else None
        )
        for emb_ref, target_ref in new_edges:
            raw = self.payloads.get(emb_ref)
            if raw is None:
                continue  # dangling edge -- matches VectorLens.project()'s skip
            n = len(raw) // 4
            if dim is None:
                dim = n
            elif n != dim:
                # Mixed embedding dimensions in the same log -- bail out to
                # the safe pure-Python path rather than build a ragged matrix.
                self._vector_matrix = None
                return None
            row = struct.unpack(f"<{n}f", raw)
            if not _is_finite_nonzero_row(row):
                # T7.1-real (D32) NaN guard: a corrupt/zero-norm embedding
                # must NEVER enter the matrix -- it would poison every
                # later cosine score in the same matmul (the numpy
                # "invalid value encountered in matmul" defect). Silently
                # excluded from ranking; a maintenance sweep re-embeds it.
                continue
            new_refs.append(target_ref)
            new_rows.append(row)

        if new_rows:
            new_mat = np.asarray(new_rows, dtype=np.float32)
            mat = (
                new_mat
                if prior_mat is None or prior_mat.shape[0] == 0
                else np.vstack([prior_mat, new_mat])
            )
            refs = prior_refs + new_refs
        else:
            mat = (
                prior_mat
                if prior_mat is not None
                else np.zeros((0, 0), dtype=np.float32)
            )
            refs = prior_refs

        self._matrix_state = (refs, mat, len(edges))
        self._vector_matrix = (refs, mat)
        return self._vector_matrix

    def scoped_vector_matrix(self, scope_ref: Any) -> tuple[list[str], Any] | None:
        """T6.1 (D29): incrementally-extendable ``(target_refs, matrix)``
        restricted to embeddings whose target carries a DIRECT ``SCOPED_TO``
        edge to *scope_ref* -- so a wing/room-scoped RRF query costs
        O(size of the scope), not O(store): unlike :meth:`vector_matrix`
        (which always builds/extends the GLOBAL matrix), this partition is
        built per scope_ref and only ever grows by embedding edges whose
        target is a member of *that* scope.

        Extended from a PRIOR fold's own ``_scope_matrix_states`` entry for
        this *scope_ref*, exactly like :meth:`vector_matrix` extends its
        global counterpart: the FIRST query against a given scope pays one
        full scan of :meth:`embedding_edges` (unavoidable -- nothing has
        been partitioned for it yet); every later query against the SAME
        scope, on a fold chain that stays intact (the store's
        snapshot/prior-fold retention), only processes the newly appended
        tail. Returns ``None`` on the same conditions as
        :meth:`vector_matrix` (no numpy, or mixed embedding dimensions
        within this scope) -- callers fall back to the pure-Python
        ``VectorLens`` path in that case.
        """
        try:
            import numpy as np
        except ImportError:
            return None

        edges = self.embedding_edges()
        state = self._scope_matrix_states.get(scope_ref)
        if state is not None and state[2] <= len(edges):
            prior_refs, prior_mat, prior_count = state
            new_edges = edges[prior_count:]
        else:
            prior_refs, prior_mat, prior_count = [], None, 0
            new_edges = edges

        new_refs: list[str] = []
        new_rows: list[tuple[float, ...]] = []
        dim = (
            prior_mat.shape[1] if prior_mat is not None and prior_mat.shape[0] else None
        )
        for emb_ref, target_ref in new_edges:
            if scope_ref not in self.scope_membership.get(target_ref, ()):
                continue
            raw = self.payloads.get(emb_ref)
            if raw is None:
                continue
            n = len(raw) // 4
            if dim is None:
                dim = n
            elif n != dim:
                self._scope_matrix_states.pop(scope_ref, None)
                return None
            row = struct.unpack(f"<{n}f", raw)
            if not _is_finite_nonzero_row(row):
                # NaN guard (T7.1-real, D32) -- see vector_matrix()'s twin
                # check; never let a corrupt/zero-norm vector into a
                # per-scope matrix either.
                continue
            new_refs.append(target_ref)
            new_rows.append(row)

        if new_rows:
            new_mat = np.asarray(new_rows, dtype=np.float32)
            mat = (
                new_mat
                if prior_mat is None or prior_mat.shape[0] == 0
                else np.vstack([prior_mat, new_mat])
            )
            refs = prior_refs + new_refs
        else:
            mat = (
                prior_mat
                if prior_mat is not None
                else np.zeros((0, 0), dtype=np.float32)
            )
            refs = prior_refs

        self._scope_matrix_states[scope_ref] = (refs, mat, len(edges))
        return (refs, mat)

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

    def ordered_passages_of(self, drawer_ref: Any) -> list[Any]:
        """P8 (D34): *drawer_ref*'s passage refs in SPAN order (ascending
        ``start``) -- the reverse of :meth:`objects_pointing_to` for
        ``@memory:passage_of``, but WITHOUT that method's alphabetic
        ``sorted()`` (rank-on-passages/read-on-neighborhoods needs the
        reading order, not ref order).

        ``_by_predicate_object[(predicate, obj)]`` already preserves EVENT
        order (each subject is appended once, on first sight -- see
        :meth:`_merge_triples`/:meth:`apply_tail_in_place`), and
        :meth:`NativeMemoryStore._write_passages` files a drawer's passages
        sequentially in ascending ``start`` order (:func:`_split_into_passages`
        yields them that way) -- so event order and span order coincide with
        no extra bookkeeping. Write-through maintained for free: this reads
        the SAME ``_by_predicate_object``/``_triple_history`` structures
        :meth:`apply_tail_in_place` already extends in place at write time,
        so no log read (T7.1/D33's invariant, unchanged) and no additional
        index to keep in sync.
        """
        out: list[Any] = []
        for subj in self._by_predicate_object.get((_MEMORY_PASSAGE_OF, drawer_ref), []):
            events = self._triple_history.get(
                (subj, _MEMORY_PASSAGE_OF, drawer_ref), []
            )
            if events and max(events, key=lambda e: e[0])[1] == "assert":
                out.append(subj)
        return out


def _is_finite_nonzero_row(row: tuple[float, ...]) -> bool:
    """T7.1-real (D32) NaN guard: whether *row* (a raw unpacked embedding)
    is safe to add to a vector matrix -- every component finite AND the
    vector not all-zero (a zero-norm row divides by zero at cosine-score
    time). Deliberately pure Python (no numpy import) so it's cheap to
    call on every single row appended one at a time, at write time.
    """
    import math

    saw_nonzero = False
    for x in row:
        if math.isnan(x) or math.isinf(x):
            return False
        if x != 0.0:
            saw_nonzero = True
    return saw_nonzero


def _fast_vector_query(
    fold: _SearchFold, query_vector: list[float], k: int, scope_ref: Any
) -> list[tuple[str, float]] | None:
    """Numpy-accelerated top-``k`` cosine search over *fold*'s embeddings
    (perf/incremental-fold), or ``None`` to signal "fall back to
    ``VectorLens.query()``" (numpy unavailable, no embeddings, or
    inconsistent embedding dimensions -- see :meth:`_SearchFold.vector_matrix`).

    Same contract as ``VectorLens.query(...).output``: ``[(target_ref,
    score)]``, descending score, ties broken by ascending ``target_ref``,
    truncated to *k*, scope filtering applied BEFORE scoring/top-k.

    T6.1 (D29): a scoped call (*scope_ref* given) reads
    :meth:`_SearchFold.scoped_vector_matrix` -- a partition built and
    incrementally extended FOR that scope alone -- instead of building/
    filtering the GLOBAL matrix, so a wing/room-scoped query costs
    O(size of the scope), not O(store).
    """
    result = (
        fold.scoped_vector_matrix(scope_ref)
        if scope_ref is not None
        else fold.vector_matrix()
    )
    if result is None:
        return None
    sub_refs, sub_mat = result
    if sub_mat.shape[0] == 0:
        return []

    import numpy as np

    q = np.asarray(query_vector, dtype=np.float64)
    if q.shape[0] != sub_mat.shape[1]:
        raise ValueError(
            f"dimension mismatch: query has {q.shape[0]}, stored has {sub_mat.shape[1]}"
        )
    if not np.all(np.isfinite(q)):
        # T7.1-real (D32) NaN guard: a non-finite QUERY vector (a
        # degenerate embed() call, e.g. on empty/malformed text) would
        # contaminate every row of `dots` via matmul -- treated the same
        # as a zero-norm query below (every score 0.0), not propagated.
        q = np.zeros_like(q)
    norms = np.linalg.norm(sub_mat, axis=1)
    qnorm = float(np.linalg.norm(q))
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        dots = sub_mat @ q
        scores = np.where((norms == 0) | (qnorm == 0), 0.0, dots / (norms * qnorm))

    order = sorted(range(len(sub_refs)), key=lambda i: (-scores[i], sub_refs[i]))
    top = order[: max(k, 0)]
    return [(sub_refs[i], float(scores[i])) for i in top]


def _embedding_is_current(fold: _SearchFold, ref: Any, current_model_id: str) -> bool:
    """T6.5 (D29): whether *ref*'s recorded ``embedded_with`` value(s)
    include *current_model_id* -- a ref with NO recorded value at all
    (pre-T6.5 content) is always treated as current, per D29's rule that
    a missing model id must never exclude otherwise-valid content."""
    values = fold.current_objects(ref, _EMBEDDED_WITH_PREDICATE)
    if not values:
        return True
    for v in values:
        raw = fold.payloads.get(v)
        if (
            raw is not None
            and raw.decode("utf-8", errors="replace") == current_model_id
        ):
            return True
    return False


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
        passage_min_chars: int = DEFAULT_PASSAGE_MIN_CHARS,
        passage_chars: int = DEFAULT_PASSAGE_CHARS,
        passage_overlap: int = DEFAULT_PASSAGE_OVERLAP,
        max_passages_per_drawer: int = DEFAULT_MAX_PASSAGES_PER_DRAWER,
        rerank: str = "off",
        rerank_top_n: int = DEFAULT_RERANK_TOP_N,
        rerank_max_chars: int = DEFAULT_RERANK_MAX_CHARS,
        rerank_config: dict[str, Any] | None = None,
        payload_lru_size: int = 4096,
    ) -> None:
        # T7.1-real (D32): the durable log's OWN path, kept ONLY for the
        # direct in-process backend (the daemon's real production path --
        # RemoteStore/GatewayClient proxy through a server that owns the
        # file itself). Used purely for a cheap `os.stat()` external-write
        # detector (see `_external_write_detected`) -- never opened or
        # read directly by this class.
        self._durable_path: str | None = (
            path if (store is None and base_url is None and token is None) else None
        )
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
        # perf/startup-latency: incremental per-event cell-ref memo shared by
        # every fold snapshot this store takes (see _CellRefCache).
        self._ref_cache = _CellRefCache()
        self._snapshot_lock = threading.Lock()
        # T7.1-real (D32): the persistent, write-through derived index --
        # built once (cold, from the kernel) and then EXTENDED IN PLACE
        # forever, either synchronously by this store's own writes
        # (``_apply_write_through``, no kernel read at all) or, rarely, by
        # a background catch-up thread when an external writer is
        # detected (see ``_external_write_detected``/
        # ``_schedule_background_catchup``). Never dropped on idle -- see
        # ``_fold_snapshot``'s docstring for the full contract and the
        # measured defect (D31: 384-460s on a 1 GB store after a 6s idle
        # gap) this supersedes. Mutations are guarded by ``_index_lock``
        # because ``search()`` deliberately runs WITHOUT the daemon's
        # write lock.
        self._prior_fold: _SearchFold | None = None
        self._last_build_at: float = 0.0
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
        # T6.1 (D29): per-scope-ref (wing OR room ref) partitioned BM25
        # indexes -- built/extended lazily, one per scope actually queried
        # (see `_search_rrf`). Unscoped queries keep using the global
        # union index above.
        self._scope_bm25_indexes: dict[Any, Any] = {}
        # T6.2 (D29): passage-splitting config -- see `file()`/
        # `_write_passages`/`ensure_passages`.
        self.passage_min_chars = passage_min_chars
        self.passage_chars = passage_chars
        self.passage_overlap = passage_overlap
        self.max_passages_per_drawer = max_passages_per_drawer
        # T6.4 (D29): local cross-encoder rerank config -- see `search()`.
        # `rerank_max_chars` is applied as the default `get_reranker()` sees
        # (measured budget: DEFAULT_RERANK_MAX_CHARS/DEFAULT_RERANK_TOP_N);
        # an explicit `rerank_config["rerank_max_chars"]` still wins.
        self.rerank_mode = rerank
        self.rerank_top_n = rerank_top_n
        self._rerank_config: dict[str, Any] = {
            "rerank_max_chars": rerank_max_chars,
            **(rerank_config or {}),
        }
        self.last_search_stats: dict[str, Any] = {}
        # T7.1-real (D32): write-through concurrency + no-log-read search.
        #
        # `_index_lock` guards every MUTATION of `_prior_fold`'s metadata
        # dicts (write-through appends AND the rare background catch-up
        # swap) -- `search()` itself runs WITHOUT this lock (the daemon's
        # own design: a slow write must never block a reader), so any
        # in-place dict mutation that could be observed mid-iteration by a
        # concurrent reader must happen only while holding it. Plain
        # `.get()` reads need no lock (safe under the GIL).
        self._index_lock = threading.RLock()
        # `(size, mtime_ns)` of the durable log file as of the last time
        # THIS process's own write-through brought the index fully
        # current. A search compares a fresh, cheap `os.stat()` against
        # this baseline instead of reading the log at all: unchanged means
        # "nothing happened since we last applied our own writes"; changed
        # means an EXTERNAL writer (a second process sharing this file --
        # tests/benchmarks only; production is single-writer) appended
        # without going through this store's write-through path.
        self._path_baseline: tuple[int, int] | None = None
        self._catchup_lock = threading.Lock()
        self._catchup_running = False
        # Bounded LRU for drawer/passage CONTENT bytes (the bulk of a real
        # store's RAM) -- populated at write time (no kernel round trip
        # for content we just wrote ourselves) and consulted before a
        # kernel resolve on a genuine miss (old content this process
        # hasn't touched recently). Small structural payloads (facts,
        # filings, scope names) stay in `_SearchFold.payloads` itself
        # (unbounded, but tiny -- a few dozen bytes each).
        self._content_lru: OrderedDict[Any, bytes] = OrderedDict()
        self._content_lru_lock = threading.Lock()
        self._content_lru_size = max(1, payload_lru_size)
        # In-process external-write signal (complements the durable-path
        # os.stat check, which only helps across separate processes): a
        # kernel observer that fires for EVERY appended event, in-process
        # or not. `_own_commit_depth` suppresses the signal for events
        # THIS store's own `_commit_and_apply` is in the middle of
        # committing (write-through already handles those synchronously,
        # right after `commit()` returns) -- anything else (e.g. a raw
        # `store.assert_fact`/`write_cell` call bypassing this class's own
        # write path entirely, as the daemon's low-level generic dispatch
        # tools do) sets `_external_dirty`, triggering the SAME background
        # catch-up used for a genuinely external process.
        self._observing: bool | None = None
        self._own_commit_depth = 0
        self._external_dirty = False

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
        embedding_model_id: str | None = None,
        filed_at: str | None = None,
        session_id: str | None = None,
        commit: str | None = None,
        embedder: Any | None = None,
    ) -> Any:
        """Persist one drawer; returns the content-addressed cell ref.

        ``embedding_model_id`` (T6.5, D29), when given alongside
        *embedding*, records ``embedded_with`` on the drawer -- lets a
        later model swap tell this vector's origin apart from one
        produced by a different embedder (search excludes a mismatch from
        the semantic arm; :meth:`requeue_stale_embeddings` marks it for
        re-embedding).

        ``embedder`` (T6.2, D29), when given, is an object exposing
        ``ready: bool`` / ``embed(text) -> Sequence[float]`` /
        ``model_id: str`` (the daemon's ``FastEmbedEmbedder`` shape) --
        used ONLY to embed passages this call creates for long *content*
        (see :meth:`_write_passages`); it never affects the drawer's own
        *embedding*, which callers compute and pass in themselves exactly
        as before.

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
            wing_scope_ref = b.write_cell(f"wing:{wing}".encode())
            b.scope(ref, wing_scope_ref)
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
                if embedding_model_id is not None:
                    b.assert_fact(
                        ref,
                        _EMBEDDED_WITH_PREDICATE,
                        b.write_cell(embedding_model_id.encode()),
                    )
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
            # T7.2 (D32): stage this drawer's passages on the SAME batch
            # (one append_batch for the drawer + its facts + all its
            # passages) instead of a second commit after this one.
            passage_stage: list[Any] = []
            if len(content) >= self.passage_min_chars:
                passage_stage = self._write_passages(
                    ref, content, wing=wing, room=room, embedder=embedder, batch=b
                )
            commit_result = self._commit_and_apply(b, wing_ref=wing_scope_ref)
            ref = _resolve_batch_ref(commit_result, ref)
            if passage_stage:
                # Resolved for parity with the sequential path's return
                # value shape, even though nothing in this module currently
                # reads _write_passages's return from `file`'s own call.
                passage_stage = [
                    _resolve_batch_ref(commit_result, r) for r in passage_stage
                ]
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
                if embedding_model_id is not None:
                    s.assert_fact(  # type: ignore[attr-defined]
                        ref,
                        _EMBEDDED_WITH_PREDICATE,
                        s.write_cell(embedding_model_id.encode()),
                    )
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
            # Sequential path: T7.2's shared-batch passage staging only
            # applies to the atomic branch above (which already wrote its
            # passages, in the SAME commit, before reaching here).
            if len(content) >= self.passage_min_chars:
                self._write_passages(
                    ref, content, wing=wing, room=room, embedder=embedder
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
            commit_result = self._commit_and_apply(b)
            filing_ref = _resolve_batch_ref(commit_result, filing_ref)
        else:
            filing_ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.assert_fact(drawer_ref, _MEMORY_FILED_AS, filing_ref)  # type: ignore[attr-defined]
        return filing_ref

    def _write_passages(
        self,
        drawer_ref: Any,
        content: str,
        *,
        wing: str,
        room: str,
        embedder: Any | None,
        batch: Any | None = None,
    ) -> list[Any]:
        """T6.2 (D29): split *content* into overlapping passages
        (:func:`_split_into_passages`) and file each as its own
        content-addressed cell, scoped identically to *drawer_ref* and
        linked back to it via ``@memory:passage_of``.

        Idempotent under content addressing: re-filing the same drawer
        content always produces the SAME passage refs/edges (a cheap
        no-op on the underlying store, since every write here is itself
        content-addressed or an idempotent ``scope``/``assert_fact``).

        *embedder*, when given and ready, embeds each passage inline
        (mirrors the daemon's embed-on-write path for drawers) and
        records ``embedded_with`` (T6.5). When *embedder* is
        ``None``/not ready/raises, a passage is marked ``needs_embedding``
        instead -- the SAME catch-up sweep that already exists for
        drawers (daemon.py's ``_sweep_needs_embedding``) embeds it once an
        embedder warms, with no daemon change required: that sweep works
        off the predicate alone, not the cell's kind.

        T7.2 (D32) -- one append per drawer+passages: when *batch* is
        given (:meth:`file`'s own atomic branch passes its SHARED
        ``WriteBatch``), every passage cell/edge is staged onto THAT batch
        instead of committing separately -- the drawer, its facts, and all
        its passages land in ONE ``append_batch`` call. When *batch* is
        ``None`` (the standalone/backfill caller, :meth:`ensure_passages`)
        this method stages its OWN batch and commits it once (still one
        append for however many passages one drawer has), falling back to
        the pre-T7.2 sequential calls only when the backend has no
        ``write_batch`` primitive at all (``RemoteStore``).
        """
        passages = _split_into_passages(
            content, passage_chars=self.passage_chars, overlap=self.passage_overlap
        )
        if not passages:
            return []
        s = self.store
        own_batch = batch is None and self._supports_atomic_update()
        b = batch if batch is not None else (s.write_batch() if own_batch else None)  # type: ignore[attr-defined]
        passage_refs: list[Any] = []
        if b is not None:
            wing_scope = b.write_cell(f"wing:{wing}".encode())
            room_scope = b.write_cell(f"room:{room}".encode())
            for start, end, text in passages:
                payload = json.dumps(
                    {
                        "kind": "passage",
                        "drawer": drawer_ref,
                        "start": start,
                        "end": end,
                        "text": text,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                ref = b.write_cell(payload)
                b.scope(ref, wing_scope)
                b.scope(ref, room_scope)
                b.assert_fact(ref, _MEMORY_PASSAGE_OF, drawer_ref)
                vector: Sequence[float] | None = None
                if embedder is not None and getattr(embedder, "ready", False):
                    try:
                        vector = embedder.embed(text)
                    except Exception:  # loud-but-graceful (KG-N3 pattern)
                        vector = None
                if vector is not None:
                    # Byte-identical to add_embedding's own packing -- a
                    # WriteBatch has no add_embedding sugar (envelope.py),
                    # so it is staged manually here exactly like `file`'s
                    # own atomic branch does for the drawer's embedding.
                    from amplifier_data.lenses.vector import EMBEDDING_OF

                    vec = list(vector)
                    emb_ref = b.write_cell(struct.pack(f"<{len(vec)}f", *vec))
                    b.relate(emb_ref, ref, EMBEDDING_OF)
                    model_id = getattr(embedder, "model_id", None)
                    if model_id is not None:
                        b.assert_fact(
                            ref,
                            _EMBEDDED_WITH_PREDICATE,
                            b.write_cell(str(model_id).encode()),
                        )
                else:
                    b.assert_fact(
                        ref, "needs_embedding", b.write_cell(_TRUE_CELL_BYTES)
                    )
                passage_refs.append(ref)
            if own_batch:
                commit_result = self._commit_and_apply(b, wing_ref=wing_scope)
                passage_refs = [
                    _resolve_batch_ref(commit_result, r) for r in passage_refs
                ]
            return passage_refs
        # Sequential fallback (no write_batch support) -- unchanged from
        # before T7.2: one call per cell/edge.
        wing_scope = s.write_cell(f"wing:{wing}".encode())  # type: ignore[attr-defined]
        room_scope = s.write_cell(f"room:{room}".encode())  # type: ignore[attr-defined]
        for start, end, text in passages:
            payload = json.dumps(
                {
                    "kind": "passage",
                    "drawer": drawer_ref,
                    "start": start,
                    "end": end,
                    "text": text,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            ref = s.write_cell(payload)  # type: ignore[attr-defined]
            s.scope(ref, wing_scope)  # type: ignore[attr-defined]
            s.scope(ref, room_scope)  # type: ignore[attr-defined]
            s.assert_fact(ref, _MEMORY_PASSAGE_OF, drawer_ref)  # type: ignore[attr-defined]
            vector = None
            if embedder is not None and getattr(embedder, "ready", False):
                try:
                    vector = embedder.embed(text)
                except Exception:  # loud-but-graceful (KG-N3 pattern)
                    vector = None
            if vector is not None:
                s.add_embedding(ref, list(vector))  # type: ignore[attr-defined]
                model_id = getattr(embedder, "model_id", None)
                if model_id is not None:
                    s.assert_fact(  # type: ignore[attr-defined]
                        ref,
                        _EMBEDDED_WITH_PREDICATE,
                        s.write_cell(str(model_id).encode()),
                    )
            else:
                s.assert_fact(  # type: ignore[attr-defined]
                    ref, "needs_embedding", s.write_cell(_TRUE_CELL_BYTES)
                )
            passage_refs.append(ref)
        return passage_refs

    def has_passages(self, ref: Any, fold: _SearchFold | None = None) -> bool:
        """Whether *ref* already has at least one ``@memory:passage_of``
        child (T6.2) -- used both to skip a long drawer's own hit in
        passage-granularity search (its passages represent it instead) and
        as the backfill sweep's idempotency check (:meth:`ensure_passages`).
        """
        if fold is not None:
            return bool(fold.objects_pointing_to(ref, _MEMORY_PASSAGE_OF))
        res = self.store.query_facts(predicate=_MEMORY_PASSAGE_OF)  # type: ignore[attr-defined]
        return any(f.object == ref for f in res.output)

    def ordered_passages_of(
        self, drawer_ref: Any, fold: _SearchFold | None = None
    ) -> list[Any]:
        """P8 (D34): *drawer_ref*'s passage refs in span order -- see
        :meth:`_SearchFold.ordered_passages_of`. Falls back to a direct
        (log-reading) ``query_facts`` scan, sorted by each passage's own
        ``start`` (the same data the ordered fold path uses, just resolved
        the slow way), only when no fold is available at all (a remote
        backend with no foldable kernel) -- the hot path (a fold snapshot
        is always available for the in-process/daemon backend this module
        is actually used with) never takes it.
        """
        if fold is not None:
            return fold.ordered_passages_of(drawer_ref)
        res = self.store.query_facts(predicate=_MEMORY_PASSAGE_OF)  # type: ignore[attr-defined]
        candidates = [f.subject for f in res.output if f.object == drawer_ref]
        bodies = [(ref, self._passage_body(ref, None)) for ref in candidates]
        bodies = [(ref, b) for ref, b in bodies if b is not None]
        bodies.sort(key=lambda item: int(item[1].get("start", 0)))
        return [ref for ref, _body in bodies]

    def _passage_body(
        self, ref: Any, fold: _SearchFold | None
    ) -> dict[str, Any] | None:
        """Decoded ``{"kind": "passage", "drawer", "start", "end", "text"}``
        payload for *ref*, or ``None`` if it isn't a passage cell (malformed
        JSON, or a ref that just doesn't carry that shape)."""
        try:
            body = json.loads(self._payload_text(ref, fold))
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict) or body.get("kind") != "passage":
            return None
        return body

    def passages_around(
        self,
        drawer_ref: Any,
        *,
        span: Sequence[int] | None = None,
        before: int = 1,
        after: int = 1,
    ) -> list[dict[str, Any]]:
        """P8 (D34): *drawer_ref*'s passages, in span order, around the
        passage covering *span* (or every passage, when *span* is
        ``None``) -- ``before``/``after`` passages either side of that
        center. Each item is ``{"ref", "span": [start, end], "content"}``.

        A drawer with no passages (short content, T6.2's
        ``passage_min_chars`` floor) returns ITSELF as the one item,
        spanning its whole content -- callers never need a separate
        short-drawer branch.

        Uses only the write-through indexes (:meth:`ordered_passages_of`,
        which reads :meth:`_SearchFold.ordered_passages_of`) plus content
        already resolved through the bounded LRU / fold payload join
        (:meth:`_payload_text`) -- no log read, matching T7.1/D33's
        contract for the rest of the search path.
        """
        fold = self._fold_snapshot()
        ordered = self.ordered_passages_of(drawer_ref, fold)
        if not ordered:
            content = self._payload_text(drawer_ref, fold)
            return [
                {"ref": str(drawer_ref), "span": [0, len(content)], "content": content}
            ]
        bodies: list[tuple[Any, dict[str, Any]]] = []
        for ref in ordered:
            body = self._passage_body(ref, fold)
            if body is not None:
                bodies.append((ref, body))
        if not bodies:
            content = self._payload_text(drawer_ref, fold)
            return [
                {"ref": str(drawer_ref), "span": [0, len(content)], "content": content}
            ]
        if span is None:
            selected = bodies
        else:
            target_start = int(span[0])
            center_idx = None
            for i, (_ref, body) in enumerate(bodies):
                start = int(body.get("start", 0))
                end = int(body.get("end", start))
                if start <= target_start < end:
                    # Keep the LATEST (highest-start) covering passage: T6.2's
                    # overlap means an earlier passage can also cover this
                    # position, but a caller passing a hit's own [start, end]
                    # span expects THAT passage, not an earlier overlapping
                    # one -- the highest start is always the most specific
                    # match.
                    center_idx = i
            if center_idx is None:
                center_idx = min(
                    range(len(bodies)),
                    key=lambda i: abs(int(bodies[i][1].get("start", 0)) - target_start),
                )
            lo = max(0, center_idx - max(0, before))
            hi = min(len(bodies), center_idx + max(0, after) + 1)
            selected = bodies[lo:hi]
        return [
            {
                "ref": str(ref),
                "span": [body.get("start"), body.get("end")],
                "content": body.get("text", ""),
            }
            for ref, body in selected
        ]

    def _passage_context(
        self,
        passage_ref: Any,
        drawer_ref: Any,
        fold: _SearchFold | None,
        *,
        expand: str,
        expand_neighbors: int,
    ) -> tuple[str, int, int]:
        """The expanded text (and its ``[start, end]`` span in the parent
        drawer's own content) for one passage hit (P8/D34): the whole
        parent drawer (``expand="drawer"``), or *passage_ref*'s own passage
        plus ``expand_neighbors`` either side, merged by taking the
        drawer's original content sliced from the earliest included
        passage's ``start`` to the latest's ``end`` -- overlap between
        consecutive passages (T6.2's ``passage_overlap``) is naturally
        deduped this way, since it re-derives the exact original substring
        rather than concatenating each passage's own (overlapping) text.
        The returned span lets a caller (e.g. the AMB adapter) merge
        several hits' windows of the SAME drawer without re-parsing text.
        """
        drawer_text = self._payload_text(drawer_ref, fold)
        if expand == "drawer":
            return drawer_text, 0, len(drawer_text)
        ordered = self.ordered_passages_of(drawer_ref, fold)
        if not ordered:
            return drawer_text, 0, len(drawer_text)
        try:
            idx = ordered.index(passage_ref)
        except ValueError:
            return drawer_text, 0, len(drawer_text)
        lo = max(0, idx - max(0, expand_neighbors))
        hi = min(len(ordered), idx + max(0, expand_neighbors) + 1)
        starts: list[int] = []
        ends: list[int] = []
        for ref in ordered[lo:hi]:
            body = self._passage_body(ref, fold)
            if body is None:
                continue
            starts.append(int(body.get("start", 0)))
            ends.append(int(body.get("end", len(drawer_text))))
        if not starts:
            return drawer_text, 0, len(drawer_text)
        start, end = min(starts), max(ends)
        return drawer_text[start:end], start, end

    def _expand_context(
        self,
        hits: list[dict[str, Any]],
        fold: _SearchFold | None,
        *,
        expand: str,
        expand_neighbors: int,
        expand_char_budget: int,
    ) -> int:
        """P8 (D34) "rank on passages, read on neighborhoods": mutate *hits*
        IN PLACE, adding a ``"context"`` field to each passage/drawer hit,
        IN RANK ORDER, until *expand_char_budget* chars have been spent --
        a hit beyond the budget keeps only its own passage (no ``"context"``
        key at all). Ranking, scores and hit order are never touched here;
        this only adds a field. Returns the total chars spent.

        A fact hit (no drawer/passage shape) is left alone. The first hit
        expanded is never rejected for being oversized (mirrors the AMB
        adapter's own budget-packing rule, T7.3) -- there must always be at
        least one expanded hit when expansion was requested and there is
        at least one eligible hit; every later hit is skipped once it would
        push the running total over budget.
        """
        if fold is None:
            return 0
        budget_used = 0
        for hit in hits:
            layer = hit.get("layer")
            span: tuple[int, int] | None = None
            if layer == "passage":
                drawer_ref = hit.get("drawer_ref")
                if drawer_ref is None:
                    continue
                context, start, end = self._passage_context(
                    hit.get("ref"),
                    drawer_ref,
                    fold,
                    expand=expand,
                    expand_neighbors=expand_neighbors,
                )
                span = (start, end)
            elif layer == "drawer":
                context = hit.get("content", "")
                content_span = hit.get("span")
                span = (
                    (int(content_span[0]), int(content_span[1]))
                    if isinstance(content_span, (list, tuple))
                    and len(content_span) == 2
                    else (0, len(context))
                )
            else:
                continue
            cost = len(context)
            if budget_used and budget_used + cost > expand_char_budget:
                continue
            hit["context"] = context
            if span is not None:
                hit["context_span"] = [span[0], span[1]]
            budget_used += cost
        return budget_used

    def ensure_passages(
        self,
        ref: Any,
        content: str,
        *,
        wing: str,
        room: str,
        embedder: Any | None = None,
    ) -> list[Any]:
        """T6.2 backfill: create passages for a pre-existing long drawer
        that has none yet. A no-op (returns ``[]``) when *content* is
        below :attr:`passage_min_chars` or *ref* already has passages --
        the daemon's bounded backfill sweep can call this unconditionally
        per candidate drawer without double-writing.
        """
        if len(content) < self.passage_min_chars:
            return []
        if self.has_passages(ref):
            return []
        return self._write_passages(
            ref, content, wing=wing, room=room, embedder=embedder
        )

    def requeue_stale_embeddings(
        self, current_model_id: str, *, limit: int = 500
    ) -> int:
        """T6.5 (D29): mark ``needs_embedding`` on every ref whose recorded
        ``embedded_with`` value differs from *current_model_id* -- the
        existing catch-up sweep (daemon.py's ``_sweep_needs_embedding``)
        then re-embeds it with the active model on its next run.

        A maintenance sweep the daemon schedules explicitly (e.g. after an
        embedder-model config change), never called from :meth:`search`
        itself (a read path must not write, D4). Bounded by *limit* per
        call so a large corpus mid-migration is processed incrementally
        across several calls instead of in one long pause. A ref with
        MULTIPLE recorded ``embedded_with`` values (re-embedded more than
        once) is requeued only if NONE of them match -- see
        :meth:`search`'s own use of the same "any current value matches"
        rule for the semantic-arm filter.
        """
        s = self.store
        res = s.query_facts(predicate=_EMBEDDED_WITH_PREDICATE)  # type: ignore[attr-defined]
        by_subject: dict[Any, set[str]] = {}
        for f in res.output:
            by_subject.setdefault(f.subject, set()).add(
                self._payload_text(f.object, None)
            )
        requeued = 0
        for subject, values in by_subject.items():
            if current_model_id in values:
                continue
            s.assert_fact(  # type: ignore[attr-defined]
                subject, "needs_embedding", s.write_cell(_TRUE_CELL_BYTES)
            )
            requeued += 1
            if requeued >= limit:
                break
        return requeued

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

    def _anchor(self, name: str, *, create: bool = True) -> Any:
        """Content-addressed anchor cell for a string KG entity (``entity:{name}``).

        KG entities are strings; substrate facts are ``(Hash, str, Hash)``.
        Content addressing makes this mapping deterministic, idempotent, and
        collision-free against the existing ``wing:``/``room:`` scope cells.

        ``create=False`` (read paths) computes the same ref without appending
        a duplicate cell to the log -- see :meth:`_read_ref`.
        """
        payload = f"entity:{name}".encode()
        if not create:
            return self._read_ref(payload)
        return self.store.write_cell(payload)  # type: ignore[attr-defined]

    def _read_ref(self, payload: bytes) -> Any:
        """The content address ``write_cell(payload)`` would return, for READS.

        Read paths only need the ref to look things up; if the cell was never
        written, nothing can be scoped to / asserted on it, so the lookup is
        empty either way. Computing it locally stops every search / diary /
        KG read from appending a duplicate cell to the append-only log. Only
        for a direct foldable kernel; remote backends keep the ``write_cell``
        round-trip.
        """
        if self._fold_capable():
            from amplifier_data.models import CellWriteEvent

            return CellWriteEvent(payload=payload).cell_ref()
        return self.store.write_cell(payload)  # type: ignore[attr-defined]

    def _fold_capable(self) -> bool:
        kernel = getattr(self.store, "kernel", None)
        return kernel is not None and callable(getattr(kernel, "all_events", None))

    def assert_kg(self, subject: str, predicate: str, object: str) -> None:
        """String-keyed KG assert: strings in, anchor-cell fact in the substrate.

        T7.1-real (D32): staged on a ``WriteBatch`` (when supported) purely
        so the SAME write-through path (``_apply_write_through``) can apply
        this assertion to the persistent index with no kernel read --
        behaviorally identical to the plain ``assert_fact`` call it
        replaces (one relationship event, same anchor cells).
        """
        s = self.store
        subj_ref = self._anchor(subject)
        obj_ref = self._anchor(object)
        if self._supports_atomic_update():
            b = s.write_batch()  # type: ignore[attr-defined]
            b.assert_fact(subj_ref, predicate, obj_ref)
            commit_result = self._commit_and_apply(b)
        else:
            s.assert_fact(subj_ref, predicate, obj_ref)  # type: ignore[attr-defined]

    def invalidate_kg(self, subject: str, predicate: str, object: str) -> None:
        s = self.store
        subj_ref = self._anchor(subject)
        obj_ref = self._anchor(object)
        if self._supports_atomic_update():
            from amplifier_data.lenses.temporal import INVALIDATE_PREFIX

            b = s.write_batch()  # type: ignore[attr-defined]
            b.relate(subj_ref, obj_ref, INVALIDATE_PREFIX + predicate)
            commit_result = self._commit_and_apply(b)
        else:
            s.invalidate_fact(subj_ref, predicate, obj_ref)  # type: ignore[attr-defined]

    def query_kg(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Currently-valid facts; anchor refs resolved back to entity strings.
        Verify-only read surface -- runs over the one-fold snapshot when a
        foldable kernel is available (perf/startup-latency)."""
        s = self.store
        subj_ref = self._anchor(subject, create=False) if subject is not None else None
        fold = self._fold_snapshot()
        if fold is not None:
            from amplifier_data.lenses.temporal import TemporalLens

            view = fold.for_subject(subj_ref) if subj_ref is not None else fold
            res = TemporalLens().query(
                kernel=view, subject=subj_ref, predicate=predicate
            )
        else:
            res = s.query_facts(subject=subj_ref, predicate=predicate)  # type: ignore[attr-defined]
        out: list[tuple[str, str, str]] = []
        for fact in res.output:
            subj_name = self._resolve_anchor(fact.subject, fold)
            obj_name = self._resolve_anchor(fact.object, fold)
            out.append((subj_name, fact.predicate, obj_name))
        return out

    def _resolve_anchor(self, ref: Any, fold: _SearchFold | None = None) -> str:
        """Resolve an anchor cell ref back to its ``entity:{name}`` string."""
        if fold is not None and ref in fold.payloads:
            payload = fold.payloads[ref].decode("utf-8")
        else:
            payload = self.store.regenerate(ref, record_access=False).payload.decode(
                "utf-8"
            )  # type: ignore[attr-defined]
        prefix = "entity:"
        return payload.removeprefix(prefix)

    def kg_timeline(self, subject: str) -> list[dict[str, Any]]:
        """SeqPos-ordered assert/invalidate history for one entity (wraps
        ``store.timeline(self._anchor(subject))``)."""
        s = self.store
        entries = s.timeline(self._anchor(subject, create=False))  # type: ignore[attr-defined]
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
            commit_result = self._commit_and_apply(b)
            ref = _resolve_batch_ref(commit_result, ref)
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

    def _read_scope_ref(self, kind: str, name: str) -> Any:
        """:meth:`_scope_ref` for READ paths: same ref, no log append."""
        return self._read_ref(f"{kind}:{name}".encode())

    def _fold_snapshot(self) -> _SearchFold | None:
        """The persistent, write-through derived index (T7.1-real, D32).

        ``None`` when the backend exposes no foldable kernel (RemoteStore /
        GatewayClient), signalling callers to keep the per-ref store-surface
        path (``regenerate`` / ``query_facts`` / ``graph_neighbors``)
        unchanged.

        Once warm (``_prior_fold`` built at least once -- the daemon's
        startup warm-up thread, or whichever request happens to arrive
        first), this method reads NOTHING from the kernel: a single cheap
        ``os.stat()`` of the durable log compares against the baseline
        this store's OWN write-through path maintains (see
        ``_apply_write_through`` / ``_refresh_path_baseline``).

        * Unchanged (the overwhelmingly common case: every one of this
          process's own writes already updated the index AND the baseline
          synchronously, in the write call itself) -> return the current
          index immediately. No kernel read, no lock contention with a
          concurrent write beyond a plain attribute read.
        * Changed -- an EXTERNAL writer (a second process/store instance
          sharing the same durable file: tests/benchmarks only; production
          is single-writer) appended without going through THIS store's
          write-through -- schedules a BACKGROUND catch-up (the proven
          incremental-extend-from-kernel path, run off the request
          thread) and returns the CURRENT, briefly-stale index right away.
          The request is never blocked on it.

        Cold (``_prior_fold`` is still ``None``): builds fresh from the
        kernel exactly as before T7.1 -- the one unavoidable full-log read,
        ideally paid once by the daemon's warm-up thread before any real
        request ever reaches this method.
        """
        if not self._fold_capable():
            return None
        kernel = self.store.kernel
        self._ensure_observed(kernel)
        with self._index_lock:
            fold = self._prior_fold
        if fold is not None:
            if self._external_dirty:
                # In-process signal: something appended to THIS SAME
                # kernel object without going through write-through (e.g.
                # the daemon's low-level generic write_cell/assert_fact/
                # batch dispatch tools). No process boundary is crossed,
                # so there is no reason to tolerate staleness here --
                # catch up synchronously (cheap: it's the SAME in-memory
                # or already-open kernel, not a fresh process's durable
                # file) and return the NOW-current index.
                fold = self._catchup_now()
            elif self._external_write_detected():
                # Cross-process signal (durable file grew via a SEPARATE
                # store instance/process): genuinely external, so defer
                # to a background thread -- this request must never block
                # on another process's write.
                self._schedule_background_catchup()
            return fold
        with self._index_lock:
            if self._prior_fold is not None:
                return self._prior_fold
            fold = _SearchFold(kernel, self._ref_cache)
            # Eager materialization (T7.1-real): force the embedding-edge
            # list to exist NOW, at the one-time cold build, so every
            # LATER vector_matrix()/scoped_vector_matrix() call (at write
            # time, via _apply_write_through, or at search time) always
            # finds it non-None and extends incrementally -- never a
            # lazy from-scratch scan of the growing in-RAM event list on
            # some later "first touch".
            fold.embedding_edges()
            self._prior_fold = fold
            self._last_build_at = time.monotonic()
            self._external_dirty = False
        self._refresh_path_baseline()
        return fold

    def _ensure_observed(self, kernel: Any) -> None:
        """Subscribe (once) to kernel appends -- the in-process half of
        external-write detection (see ``_on_append``). Best-effort: a
        kernel without ``subscribe`` (none in practice today) just never
        sets ``_external_dirty``, falling back to the durable-path
        ``os.stat`` check alone.
        """
        if self._observing is not None:
            return
        subscribe = getattr(kernel, "subscribe", None)
        if not callable(subscribe):
            self._observing = False
            return
        # The kernel (a Rust object the cycle collector cannot traverse)
        # must not keep this store alive forever via a bound-method
        # observer -- subscribe through a weakref, exactly like the
        # pre-T7.1-real snapshot observer did.
        store_ref = weakref.ref(self)

        def _observer(pos: Any, event: Any) -> None:
            store = store_ref()
            if store is not None:
                store._on_append(pos, event)

        try:
            subscribe(_observer)
            self._observing = True
        except Exception:  # noqa: BLE001 - observation is an optimization, never required
            self._observing = False

    def _on_append(self, _pos: Any, _event: Any) -> None:
        """Kernel observer: fires for EVERY appended event. Anything NOT
        happening inside THIS store's own ``_commit_and_apply`` (which
        already applies its own events synchronously, right after
        ``commit()`` returns) is either a genuinely external writer or a
        caller bypassing this class's write path (e.g. the daemon's raw
        ``write_cell``/``assert_fact`` dispatch tools) -- either way, the
        persistent index is now stale and needs a catch-up.
        """
        if self._own_commit_depth <= 0:
            self._external_dirty = True

    def _commit_and_apply(self, batch: Any, *, wing_ref: Any = None) -> Any:
        """Commit *batch* and apply it to the persistent index
        write-through, in one step -- the ONLY place that calls
        ``batch.commit()`` for every write path in this class, so the
        "is this one of our own writes" signal (``_own_commit_depth``,
        see ``_on_append``) is centralized rather than duplicated at
        every call site.
        """
        self._own_commit_depth += 1
        try:
            commit_result = batch.commit()
        finally:
            self._own_commit_depth -= 1
        self._apply_write_through(batch, commit_result, wing_ref=wing_ref)
        return commit_result

    def _external_write_detected(self) -> bool:
        """Cheap ``os.stat()``-based signal that the durable log grew
        without going through this store's own write-through path.

        Always ``False`` when there is no durable path to stat (an
        in-memory kernel, or a remote/gateway backend that doesn't own
        the file directly) -- there is no OTHER process that could share
        that state anyway.
        """
        path = self._durable_path
        if not path:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False
        current = (st.st_size, st.st_mtime_ns)
        with self._index_lock:
            baseline = self._path_baseline
        return baseline is not None and current != baseline

    def _refresh_path_baseline(self) -> None:
        """Record the durable log's current ``(size, mtime_ns)`` as "fully
        reflected in the index" -- called after every successful
        write-through application (own writes) and after a background
        catch-up completes (external writes), so the NEXT ``os.stat()``
        compare sees "unchanged" until something ELSE appends.
        """
        path = self._durable_path
        if not path:
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        with self._index_lock:
            self._path_baseline = (st.st_size, st.st_mtime_ns)

    def _catchup_now(self, *, cross_process: bool = False) -> _SearchFold:
        """Synchronously catch the persistent index up to current state,
        using the proven incremental-extend-from-kernel path
        (:class:`_SearchFold`'s ``prior=`` constructor) -- the SAME
        mechanism T7.1 already relies on.

        *cross_process* distinguishes the two triggers (see
        ``_fold_snapshot``):

        * ``False`` (the in-process ``_external_dirty`` signal): reads via
          THIS store's own, already-open kernel handle -- correct AND
          necessary, since that handle already sees the bypassing write
          (same kernel object).
        * ``True`` (the durable-path ``os.stat`` signal, a genuinely
          separate process): reads via a FRESHLY-opened kernel handle on
          the same durable path instead. Confirmed empirically:
          ``RustFileKernel.all_events()`` on an ALREADY-OPEN handle does
          NOT observe appends made through a DIFFERENT handle to the same
          file, even after that writer closes -- only a fresh ``open()``
          sees them. Closed immediately after use; this store's own
          kernel handle (what it writes through) is never replaced.
        """
        self._external_dirty = False
        fresh_kernel = None
        if cross_process and self._durable_path:
            from amplifier_data.kernel import DurableKernel

            fresh_kernel = DurableKernel.open(self._durable_path)
            kernel = fresh_kernel
        else:
            kernel = self.store.kernel
        try:
            with self._index_lock:
                base = self._prior_fold
            fold = _SearchFold(kernel, self._ref_cache, prior=base)
            fold.embedding_edges()
        finally:
            if fresh_kernel is not None:
                fresh_kernel.close()
        with self._index_lock:
            current = self._prior_fold
            if current is None or len(fold._events) >= len(current._events):
                self._prior_fold = fold
            else:
                fold = current
        self._refresh_path_baseline()
        return fold

    def _schedule_background_catchup(self) -> None:
        """Kick off (at most one concurrent) background thread running
        :meth:`_catchup_now` -- used for the CROSS-PROCESS
        ``_external_write_detected`` signal, where a search must never
        block on another process's write.
        """
        with self._catchup_lock:
            if self._catchup_running:
                return
            self._catchup_running = True

        def _run() -> None:
            try:
                self._catchup_now(cross_process=True)
            except Exception:  # noqa: BLE001 - background best-effort, never crash the daemon
                _logger.exception("memory: background index catch-up failed")
            finally:
                with self._catchup_lock:
                    self._catchup_running = False

        threading.Thread(target=_run, daemon=True, name="memory-index-catchup").start()

    def _apply_write_through(
        self, batch: Any, commit_result: Any, *, wing_ref: Any = None
    ) -> None:
        """T7.1-real (D32): apply EXACTLY the events one write call just
        committed directly to the persistent index -- no kernel read.

        *batch* is the ``WriteBatch`` the caller staged (``.staged`` gives
        the real ``Event`` objects in commit order); *commit_result* is
        whatever ``batch.commit()`` returned. Zipping the two together
        gives ``(SeqPos, Event)`` pairs identical to what
        ``kernel.all_events()`` would eventually show -- the kernel
        assigned those exact positions during ``append_batch`` -- so
        applying them here is not an approximation, it's the same data,
        just without asking the kernel for it a second time.

        A safe no-op whenever write-through cannot apply cleanly (no
        foldable kernel, an empty/degraded batch, or a ``GatewayWriteBatch``
        pending-token commit result whose refs aren't real content
        addresses yet) -- the store stays correct either way: the next
        external-write check (or the next warm-up) picks up anything
        missed via the background catch-up path.

        *wing_ref*, when given, is the SINGLE wing scope cell this call's
        drawer/passage content is scoped to (``file()``'s shared batch) --
        used to EAGERLY extend that wing's partitioned vector matrix and
        BM25 index right now, so no later search ever pays a "first touch
        of this wing" cost.
        """
        if not self._fold_capable():
            return
        if isinstance(commit_result, dict):
            self._refresh_path_baseline()
            return
        staged = getattr(batch, "staged", None)
        if not staged:
            self._refresh_path_baseline()
            return
        positions = list(commit_result)
        if len(positions) != len(staged):
            self._refresh_path_baseline()
            return

        from amplifier_data.lenses._scope import SCOPED_TO
        from amplifier_data.lenses.vector import EMBEDDING_OF
        from amplifier_data.models import CellWriteEvent, RelationshipEvent

        events = list(zip(positions, staged))
        payload_of = {
            ev.cell_ref(): ev.payload
            for _pos, ev in events
            if isinstance(ev, CellWriteEvent)
        }
        with self._index_lock:
            fold = self._prior_fold
            if fold is not None:
                fold.apply_tail_in_place(events, payload_of)
        if fold is None:
            # Warm-up hasn't built the persistent index yet -- nothing to
            # extend; the eventual cold build reads everything from the
            # kernel anyway.
            self._refresh_path_baseline()
            return

        if wing_ref is not None:
            # Only the WING partition is eagerly primed here -- matching
            # `_search_rrf`'s actual usage (a room filter NARROWS within
            # the wing partition via `narrow_ref`; a separate room-keyed
            # BM25/vector partition is only ever built for a room-only
            # query with no wing, which write-through does not know about
            # at write time -- that case still gets it lazily, exactly as
            # before this fix). Priming both would double the tokenize
            # work for no benefit this store's own read path ever uses.
            scoped_here = {
                ev.from_ref
                for _pos, ev in events
                if isinstance(ev, RelationshipEvent)
                and ev.type == SCOPED_TO
                and ev.to_ref == wing_ref
            }
            if scoped_here:
                self._prime_bm25_for_refs(wing_ref, scoped_here, payload_of)
            for _pos, ev in events:
                if not (isinstance(ev, RelationshipEvent) and ev.type == EMBEDDING_OF):
                    continue
                target_ref = ev.to_ref
                if target_ref in scoped_here:
                    fold.scoped_vector_matrix(wing_ref)
                    fold.vector_matrix()
                    break

        # Content bytes go to the bounded LRU (immediately available for a
        # hit render without a kernel round trip), NOT into fold.payloads
        # forever -- that dict already grew by *payload_of* above via
        # apply_tail_in_place, but for content-sized cells that's exactly
        # the unbounded-RAM growth T7.1-real must avoid. Evict them from
        # the fold's payload dict right after use and let the LRU own
        # them from here on.
        if wing_ref is not None:
            content_refs = {
                ev.from_ref
                for _pos, ev in events
                if isinstance(ev, RelationshipEvent) and ev.type == SCOPED_TO
            }
            for ref in content_refs:
                raw = payload_of.get(ref)
                if raw is None:
                    continue
                self._content_lru_put(ref, raw)
                fold.payloads.pop(ref, None)

        self._refresh_path_baseline()

    def _prime_bm25_for_refs(
        self, scope_ref: Any, refs: set[Any], payload_of: dict[Any, bytes]
    ) -> None:
        """Eagerly ``.add()`` *refs* (content this write just scoped to
        *scope_ref*) into that scope's BM25 partition, using the text
        already in hand -- no payload lookup, no kernel read. Idempotent
        (``BM25Index.add`` no-ops for an already-indexed ref)."""
        if BM25Index is None or not refs:
            return
        index = self._scope_bm25_indexes.setdefault(scope_ref, BM25Index())
        for ref in refs:
            raw = payload_of.get(ref)
            if raw is None:
                continue
            index.add(ref, raw.decode("utf-8", errors="replace"))

    def _content_lru_put(self, ref: Any, raw: bytes) -> None:
        with self._content_lru_lock:
            self._content_lru[ref] = raw
            self._content_lru.move_to_end(ref)
            while len(self._content_lru) > self._content_lru_size:
                self._content_lru.popitem(last=False)

    def _content_lru_get(self, ref: Any) -> bytes | None:
        with self._content_lru_lock:
            raw = self._content_lru.get(ref)
            if raw is not None:
                self._content_lru.move_to_end(ref)
            return raw

    def eager_build_all_wings(self) -> int:
        """T7.1-real (D32): after the cold build, eagerly materialize
        EVERY wing's vector + BM25 partition (not just the ones a search
        happens to touch first) -- called by the daemon's startup warm-up
        thread. Returns the number of wings built. Idempotent (safe to
        call more than once; already-current partitions are cheap no-ops).
        """
        fold = self._fold_snapshot()
        if fold is None:
            return 0
        fold.embedding_edges()
        with self._index_lock:
            wing_refs = [
                ref
                for ref in fold.scope_reverse
                if fold.payloads.get(ref, b"").startswith(b"wing:")
            ]
        for wing_ref in wing_refs:
            with self._index_lock:
                fold.scoped_vector_matrix(wing_ref)
                members = set(fold.scope_reverse.get(wing_ref, ()))
            self._prime_bm25_for_refs(
                wing_ref,
                members,
                {ref: self._payload_bytes_for_index(ref, fold) for ref in members},
            )
        return len(wing_refs)

    def _payload_bytes_for_index(self, ref: Any, fold: _SearchFold) -> bytes:
        raw = fold.payloads.get(ref)
        if raw is not None:
            return raw
        cached = self._content_lru_get(ref)
        if cached is not None:
            return cached
        cell = self.store.get_cell(ref, record_access=False)  # type: ignore[attr-defined]
        return cell.payload

    def _payload_text(self, ref: Any, fold: _SearchFold | None) -> str:
        """Decode ``ref``'s payload: bounded content LRU first (T7.1-real,
        D32 -- populated at write time, no kernel round trip for content
        this process just wrote), then the fold's own (small, metadata-
        only) payload join, then a kernel resolve on a genuine miss
        (``get_cell`` -- uses the kernel's OWN materialized cache, so a
        repeat miss for the SAME ref is cheap; only the very first touch
        of a ref this process never wrote costs a log read).
        """
        cached = self._content_lru_get(ref)
        if cached is not None:
            return cached.decode("utf-8", errors="replace")
        if fold is not None:
            raw = fold.payloads.get(ref)
            if raw is not None:
                return raw.decode("utf-8", errors="replace")
        raw = self.store.get_cell(ref, record_access=False).payload  # type: ignore[attr-defined]
        self._content_lru_put(ref, raw)
        return raw.decode("utf-8", errors="replace")

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
        """``{"wing","room","source","category","filed_at","importance"}``
        for one drawer hit (search/list_drawers), scope-aware (see
        :meth:`_resolve_filing`); falls back to the pre-fix per-predicate
        facts + first-matching scope edge for a legacy drawer with no
        filing. ``importance`` (perf/startup-latency, a402866) is resolved
        here too -- the filing cell never carries it (see
        :meth:`_filing_payload`), so it's always one ``has_importance``
        lookup against the shared fold, regardless of filing presence."""
        importance_raw = self._first_fact_value(ref, "has_importance", fold)
        try:
            importance = float(importance_raw) if importance_raw is not None else None
        except ValueError:
            importance = None
        filing = self._resolve_filing(ref, fold, wing=wing, room=room)
        if filing is not None:
            return {
                "wing": filing.get("wing"),
                "room": filing.get("room"),
                "source": filing.get("source"),
                "category": filing.get("category"),
                "filed_at": filing.get("filed_at"),
                "importance": importance,
            }
        wing_name, room_name = self._resolve_wing_room(ref, fold)
        return {
            "wing": wing_name,
            "room": room_name,
            "source": self._first_fact_value(ref, "has_source", fold),
            "category": self._first_fact_value(ref, "has_category", fold),
            "filed_at": self.drawer_times([ref], _fold=fold, wing=wing).get(ref),
            "importance": importance,
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
            scope_ref = self._read_scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._read_scope_ref("wing", wing)
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
            out.append(
                {
                    "ref": ref,
                    "content": content,
                    "wing": meta["wing"],
                    "room": meta["room"],
                    "category": meta["category"],
                    "importance": meta["importance"],
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
        granularity: str | None = None,
        rerank: bool | None = None,
        max_passages_per_drawer: int | None = None,
        current_model_id: str | None = None,
        expand: str | None = None,
        expand_neighbors: int = DEFAULT_EXPAND_NEIGHBORS,
        expand_char_budget: int = DEFAULT_EXPAND_CHAR_BUDGET,
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

        ``granularity`` (T6.2, D29): ``"passage"`` | ``"drawer"`` | ``None``
        (server default -- ``"passage"`` whenever RRF fusion is active,
        ``"drawer"`` in legacy mode, unchanged). In passage mode, a long
        drawer (one carrying its own passage cells) is represented by its
        best-ranked PASSAGES instead of its own full text, collapsed to at
        most ``max_passages_per_drawer`` (default
        :attr:`max_passages_per_drawer`) per parent -- passage hits carry
        ``layer="passage"``, ``drawer_ref``, ``span=[start, end]``, and
        attribution resolved from the PARENT drawer's own scope-aware
        filing. In drawer mode, the best-ranked passage of a long drawer
        rolls up to that drawer's full verbatim content instead (a
        best-segment rollup) -- backward-compatible shape, one hit per
        drawer either way. Short drawers (below :attr:`passage_min_chars`,
        no passages) are unaffected by *granularity* and returned as
        ordinary ``layer="drawer"`` hits in both modes.

        ``rerank`` (T6.4, D29): ``True``/``False``/``None`` (server default,
        :attr:`rerank_mode`). When effectively on, the fused top
        :attr:`rerank_top_n` hits are re-scored by a local cross-encoder
        (``rerank.py``'s ``get_reranker``) and reordered (ties keep RRF
        order); each reranked hit gains ``rerank_score``. If the reranker
        is unavailable or raises, hits keep RRF order and gain
        ``rerank_skipped`` instead -- never a hard failure.

        ``current_model_id`` (T6.5, D29): when given, a candidate whose
        recorded ``embedded_with`` value(s) never include this model id is
        excluded from the semantic arm (its vector is stale) -- a ref with
        no recorded ``embedded_with`` at all (pre-T6.5 content) is always
        treated as current. Excluding is search-side only; a separate
        maintenance sweep (:meth:`requeue_stale_embeddings`) marks stale
        refs for re-embedding.

        ``expand`` (P8, D34) -- "rank on passages, read on neighborhoods":
        ``"none"`` (default, unchanged shape), ``"neighbors"`` or
        ``"drawer"``. When not ``"none"``, every passage/drawer hit, IN
        RANK ORDER, gains a ``"context"`` field: ``expand_neighbors``
        passages either side of the hit merged with it (``"neighbors"``),
        or the whole parent drawer (``"drawer"``) -- until
        ``expand_char_budget`` chars have been spent, after which later
        hits keep only their own passage. Ranking/scores/order are
        untouched; this only adds a field (:meth:`_expand_context`).

        Every read in this method goes through ONE log fold per call
        (:class:`_SearchFold` -- see its class docstring for the perf
        rationale this preserves: O(1) folds instead of O(reads) regenerate
        calls). The caller (the daemon dispatch layer) is responsible for
        setting the wire-level ``degraded`` flag based on whether it passed
        a real vector.
        """
        layer_set = set(layers) if layers is not None else {"fact", "drawer"}
        resolved_fusion = (
            fusion
            if fusion is not None
            else ("rrf" if BM25Index is not None else "legacy")
        )
        resolved_granularity = (
            granularity
            if granularity is not None
            else ("passage" if resolved_fusion == "rrf" else "drawer")
        )
        # Only widen the internal pool when a layer is actually excluded, or
        # passage-granularity collapsing might otherwise under-fill after
        # filtering (facts/drawers/passages share one candidate pool). The
        # default (both layers, drawer granularity) requests EXACTLY k, same
        # as pre-P2 -- T1.4's p95 budget is measured against this default
        # path and must not pay for a widening it never needed (the internal
        # RRF/legacy engines already widen their OWN sub-pools proportionally
        # to whatever k they're given, so requesting a larger k here
        # compounds).
        pool_k = (
            k
            if layer_set == {"fact", "drawer"} and resolved_granularity == "drawer"
            else max(k * 3, 50)
        )

        s = self.store
        scope_ref = None
        if room is not None:
            scope_ref = self._read_scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._read_scope_ref("wing", wing)
        # T6.1 (D29): the RRF path partitions by WING FIRST (falling back to
        # room alone when no wing is given) and applies room as a secondary
        # intersection filter -- unlike `scope_ref` above (room-priority,
        # kept EXACTLY as-is for `_search_legacy`'s byte-identical
        # contract). Two different wings that happen to reuse the same
        # room NAME (a content-addressed "room:x" cell is shared across
        # every wing using that name) would otherwise collapse into one
        # giant room-only partition covering the whole store -- the actual
        # scale defect D26/D27 measured. Room-only requests (no wing) are
        # unaffected: `rrf_partition_ref` is just the room ref, same as
        # `scope_ref`.
        wing_ref = self._read_scope_ref("wing", wing) if wing is not None else None
        room_ref = self._read_scope_ref("room", room) if room is not None else None
        rrf_partition_ref = wing_ref if wing_ref is not None else room_ref
        rrf_narrow_ref = (
            room_ref if (wing_ref is not None and room_ref is not None) else None
        )
        # Snapshot AFTER the scope-cell write above, so the fold sees at least
        # everything the per-call store-surface folds used to see.
        fold = self._fold_snapshot()

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

        t_search_start = time.monotonic()
        if use_rrf:
            raw = self._search_rrf(
                query_vector,
                pool_k,
                wing=wing,
                room=room,
                lexical_query=lexical_query,
                scope_ref=rrf_partition_ref,
                narrow_ref=rrf_narrow_ref,
                fold=fold,
                eligible=eligible,
                current_model_id=current_model_id,
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

        effective_max_passages = (
            max_passages_per_drawer
            if max_passages_per_drawer is not None
            else self.max_passages_per_drawer
        )
        filtered: list[dict[str, Any]] = []
        passages_per_drawer: dict[Any, int] = {}
        emitted_drawers: set[Any] = set()
        for hit in raw:
            ref = hit["ref"]
            kind = self._classify_ref(ref, fold)
            if kind == "fact":
                if "fact" not in layer_set:
                    continue
                if not self._is_current(ref, fold):
                    continue
                try:
                    body = json.loads(hit["content"])
                except (ValueError, TypeError):
                    body = {}
                hit = {**hit, "content": body.get("text", "")}
                hit["derived_from"] = self._derived_from_refs(ref, fold)
                hit["layer"] = "fact"
                filtered.append(hit)
            elif kind == "passage":
                if "drawer" not in layer_set:
                    continue
                try:
                    body = json.loads(hit["content"])
                except (ValueError, TypeError):
                    continue
                drawer_ref = body.get("drawer")
                if drawer_ref is None:
                    continue
                if resolved_granularity == "drawer":
                    if drawer_ref in emitted_drawers:
                        continue
                    emitted_drawers.add(drawer_ref)
                    meta = self._resolve_hit_meta(
                        drawer_ref, fold, wing=wing, room=room
                    )
                    filtered.append(
                        {
                            **hit,
                            "ref": drawer_ref,
                            "content": self._payload_text(drawer_ref, fold),
                            "layer": "drawer",
                            "wing": meta["wing"],
                            "room": meta["room"],
                            "category": meta["category"],
                            "source": meta["source"],
                            "filed_at": meta["filed_at"],
                            "importance": meta["importance"],
                        }
                    )
                else:
                    count = passages_per_drawer.get(drawer_ref, 0)
                    if count >= max(0, effective_max_passages):
                        continue
                    passages_per_drawer[drawer_ref] = count + 1
                    meta = self._resolve_hit_meta(
                        drawer_ref, fold, wing=wing, room=room
                    )
                    filtered.append(
                        {
                            **hit,
                            "layer": "passage",
                            "drawer_ref": drawer_ref,
                            "span": [body.get("start"), body.get("end")],
                            "content": body.get("text", hit["content"]),
                            "wing": meta["wing"],
                            "room": meta["room"],
                            "category": meta["category"],
                            "source": meta["source"],
                            "filed_at": meta["filed_at"],
                            "importance": meta["importance"],
                        }
                    )
            else:  # plain drawer
                if "drawer" not in layer_set:
                    continue
                if resolved_granularity == "passage" and self.has_passages(ref, fold):
                    continue  # represented via its own passages instead
                if ref in emitted_drawers:
                    continue
                emitted_drawers.add(ref)
                hit = {**hit, "layer": "drawer"}
                filtered.append(hit)
            if len(filtered) >= max(0, k):
                break

        rerank_ms = 0.0
        resolved_rerank = (
            rerank if rerank is not None else (self.rerank_mode == "cross-encoder")
        )
        if resolved_rerank and filtered:
            t_rerank_start = time.monotonic()
            top_n = min(len(filtered), max(0, self.rerank_top_n))
            try:
                if get_reranker is None:
                    raise RuntimeError("rerank.py not available")
                # get_reranker reads its own "rerank" mode key off the
                # config dict it's given; `resolved_rerank` may be True
                # via the per-call override even when the store's own
                # `rerank_mode` is "off", so force "cross-encoder" here
                # rather than relying on `self._rerank_config` alone.
                reranker = get_reranker(
                    {**self._rerank_config, "rerank": "cross-encoder"}
                )
                if reranker is None:
                    raise RuntimeError("no reranker configured")
                texts = [h["content"] for h in filtered[:top_n]]
                scores = reranker.score(lexical_query or "", texts)
                pairs = sorted(enumerate(scores), key=lambda pair: -pair[1])
                order = [i for i, _s in pairs]
                reranked = [filtered[i] for i in order] + filtered[top_n:]
                for pos, i in enumerate(order):
                    reranked[pos]["rerank_score"] = scores[i]
                filtered = reranked
            except Exception as exc:  # loud-but-graceful: keep RRF order
                for h in filtered:
                    h["rerank_skipped"] = f"{type(exc).__name__}: {exc}"
            rerank_ms = (time.monotonic() - t_rerank_start) * 1000.0

        resolved_expand = expand if expand is not None else "none"
        expand_chars_used = 0
        if resolved_expand != "none" and filtered:
            expand_chars_used = self._expand_context(
                filtered,
                fold,
                expand=resolved_expand,
                expand_neighbors=expand_neighbors,
                expand_char_budget=expand_char_budget,
            )

        self.last_search_stats = {
            "fusion": resolved_fusion,
            "granularity": resolved_granularity,
            "rerank": bool(resolved_rerank),
            "rerank_ms": rerank_ms,
            "total_ms": (time.monotonic() - t_search_start) * 1000.0,
            "expand": resolved_expand,
            "expand_chars_used": expand_chars_used,
        }
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
                    "importance": meta["importance"],
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
        current_model_id: str | None = None,
        narrow_ref: Any | None = None,
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
        from .embedder import lexical_score

        assert fold is not None
        assert BM25Index is not None

        # T6.1 (D29, scale fix): a SCOPED query reads the fold's own
        # precomputed `scope_reverse` (scope_ref -> {member ref, ...}), an
        # O(1) dict lookup sized to the SCOPE, never
        # `ScopeIndex.cells_in_scope`, which scans every ref in the whole
        # store regardless of the requested scope. An UNSCOPED query still
        # needs the full membership set (there is no smaller universe to
        # read) -- `scope_index()` is only ever called on that path.
        if scope_ref is not None:
            scoped_refs = set(fold.scope_reverse.get(scope_ref, ()))
        else:
            scope_index = fold.scope_index()
            scoped_refs = set(scope_index.membership.keys())
        if narrow_ref is not None:
            # T6.1 (D29): room-within-wing intersection -- narrows the
            # WING partition down to members ALSO scoped to the room, a
            # cheap O(partition size) filter (never O(store)).
            scoped_refs = {
                ref
                for ref in scoped_refs
                if narrow_ref in fold.scope_membership.get(ref, ())
            }
        candidate_refs = scoped_refs if eligible is None else (scoped_refs & eligible)

        # T6.1 (D29): a scoped query builds/extends a BM25Index PARTITIONED
        # to this one scope_ref (per-wing or per-room IDF -- D29 calls this
        # "fine, arguably better") from ONLY its own scoped_refs, so the
        # index-maintenance loop below costs O(scope size), never O(store).
        # An unscoped query keeps using the single persistent GLOBAL index
        # (self._bm25_index) so its corpus-wide IDF/union semantics are
        # unchanged.
        #
        # T7.1-real (D32): write-through (`_apply_write_through`) already
        # `.add()`s every wing/room-scoped drawer/passage AT WRITE TIME
        # using the text it already has in hand, so in steady state every
        # ref here is ALREADY indexed by the time a search sees it. The
        # `ref in index._doc_len` check below skips the (otherwise
        # unconditional) `_payload_text` lookup entirely for those --
        # avoiding a kernel/LRU round trip per scoped ref on EVERY search,
        # which is what made a warm, repeat-touched wing still cost
        # hundreds of ms before this fix. Only a ref this write-through
        # pass never saw (external content, or content filed before
        # warm-up finished) pays the lookup, exactly once, ever.
        if scope_ref is not None:
            index = self._scope_bm25_indexes.setdefault(scope_ref, BM25Index())
        else:
            index = self._bm25_index
        for ref in sorted(scoped_refs):
            if ref in index._doc_len:  # noqa: SLF001 - see docstring above
                continue
            index.add(ref, self._payload_text(ref, fold))

        n_pool = max(3 * k, 50)

        semantic_rank: dict[Any, int] = {}
        cosine_by_ref: dict[Any, float] = {}
        if query_vector is not None:
            # When a temporal filter narrowed the eligible set, request a
            # wider vector pool so an eligible-but-lower-cosine candidate
            # is not crowded out of the top-n_pool by ineligible ones.
            # When narrowing a wing partition down to one room, the raw
            # candidate pool must be drawn from the WHOLE wing (not the
            # already-narrowed `scoped_refs`) so filtering afterward still
            # has enough room-scoped hits to fill `n_pool`.
            wing_partition_size = (
                len(fold.scope_reverse.get(scope_ref, ()))
                if narrow_ref is not None and scope_ref is not None
                else len(scoped_refs)
            )
            vector_pool_k = (
                max(n_pool, wing_partition_size)
                if (eligible is not None or narrow_ref is not None)
                else n_pool
            )
            # perf/incremental-fold: numpy-accelerated candidate generation
            # over the fold's own vector matrix, falling back to the
            # pure-Python VectorLens when numpy/embeddings are unavailable.
            raw_candidates = _fast_vector_query(
                fold, list(query_vector), max(1, vector_pool_k), scope_ref
            )
            if raw_candidates is None:
                from amplifier_data.lenses.vector import VectorLens

                raw_candidates = (
                    VectorLens()
                    .query(
                        kernel=fold.vector_view(),
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
                if (
                    narrow_ref is not None
                    and narrow_ref not in fold.scope_membership.get(ref, ())
                ):
                    continue
                # T6.5 (D29): a vector produced by a DIFFERENT embedder than
                # the currently active one is stale -- exclude it from the
                # semantic arm rather than let it rank as if current. A ref
                # with NO recorded `embedded_with` at all (pre-T6.5 content)
                # is always treated as current.
                if current_model_id is not None and not _embedding_is_current(
                    fold, ref, current_model_id
                ):
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
                    "importance": meta["importance"],
                }
            )
        return results

    def read_diary(self, *, agent_name: str, last_n: int = 10) -> list[dict[str, Any]]:
        """Cells under scope ``agent:{name}``, SeqPos-ordered, newest last (§3.2).

        Mirrors :meth:`list_drawers`'s use of the (precomputed, D26) scope
        index for membership, then orders by each cell's OWN defining
        ``CellWriteEvent`` position in the log (SeqPos) -- the fold does not
        carry position, so this walks ``fold.all_events()``/``fold.refs``
        once to build a ``ref -> first SeqPos`` map, over the ONE-fold
        snapshot (perf/startup-latency) when a foldable kernel is available.
        """
        from amplifier_data.lenses._scope import fold_scope
        from amplifier_data.models import CellWriteEvent

        s = self.store
        scope_ref = self._read_scope_ref("agent", agent_name)
        fold = self._fold_snapshot()
        scope_index = (
            fold.scope_index() if fold is not None else fold_scope(s.kernel)  # type: ignore[attr-defined]
        )
        member_refs = scope_index.cells_in_scope(scope_ref)

        order: dict[Any, int] = {}
        if fold is not None:
            for (pos, _ev), ref in zip(fold.all_events(), fold.refs):
                if ref is not None and ref in member_refs and ref not in order:
                    order[ref] = pos
        else:
            for pos, ev in s.kernel.all_events():  # type: ignore[attr-defined]
                if isinstance(ev, CellWriteEvent):
                    ref = ev.cell_ref()
                    if ref in member_refs and ref not in order:
                        order[ref] = pos

        ordered_refs = sorted(member_refs, key=lambda r: order.get(r, 0))
        tail = ordered_refs[-max(0, last_n) :] if last_n > 0 else []

        entries: list[dict[str, Any]] = []
        for ref in tail:
            entry_text = self._payload_text(ref, fold)
            _, topic = self._resolve_wing_room(ref, fold)
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
            "passage",
            "filing",
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
            commit_result = self._commit_and_apply(b)
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
            scope_ref = self._read_scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._read_scope_ref("wing", wing)
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
            commit_result = self._commit_and_apply(b)
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
            commit_result = self._commit_and_apply(b)
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

        room_scope_ref = self._read_scope_ref("room", room)
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

        wing_scope_ref = self._read_scope_ref("wing", wing)
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
        room_scope_ref = self._read_scope_ref("room", room)
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
            commit_result = self._commit_and_apply(b)
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
            commit_result = self._commit_and_apply(b)
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
        wing_scope_ref = self._read_scope_ref("wing", wing)
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
