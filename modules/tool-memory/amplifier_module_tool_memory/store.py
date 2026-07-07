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

import struct
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from amplifier_module_tool_memory.scripts.mutation import (
    MutationRecord,
    ReversibleDelta,
    new_mutation,
)


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
    ) -> None:
        """Persist one consolidated cell.

        ``embedding``, when provided, is a pre-computed vector to transport
        alongside the cell. The seam NEVER computes embeddings itself (the
        embedder is bundle policy, per COMPOSITION.md) — it only carries a
        vector a caller already has.
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
    ) -> None:
        record: dict[str, object] = {
            "wing": wing,
            "room": room,
            "content": content,
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
    ) -> None:
        s = self.store
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

    def assert_kg(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        """String-keyed KG assert: strings in, anchor-cell fact in the substrate."""
        s = self.store
        s.assert_fact(self._anchor(subject), predicate, self._anchor(object))  # type: ignore[attr-defined]

    def invalidate_kg(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
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
        return payload[len(prefix) :] if payload.startswith(prefix) else payload

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

    def _first_fact_value(self, ref: Any, predicate: str) -> str | None:
        """Currently-valid ``predicate`` object for ``ref``, resolved to a plain string."""
        s = self.store
        res = s.query_facts(subject=ref, predicate=predicate)  # type: ignore[attr-defined]
        if not res.output:
            return None
        return s.regenerate(res.output[0].object, record_access=False).payload.decode(  # type: ignore[attr-defined]
            "utf-8", errors="replace"
        )

    def _resolve_wing_room(self, ref: Any) -> tuple[str | None, str | None]:
        """(wing, room) for a drawer ref, resolved from its direct ``scoped_to`` edges."""
        s = self.store
        wing_name: str | None = None
        room_name: str | None = None
        for n in s.graph_neighbors(ref, rel_type="scoped_to"):  # type: ignore[attr-defined]
            label = s.regenerate(n, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            if label.startswith("wing:"):
                wing_name = label[len("wing:") :]
            elif label.startswith("room:"):
                room_name = label[len("room:") :]
        return wing_name, room_name

    def list_drawers(
        self, *, wing: str | None = None, room: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Scoped drawer listing for garden/status/degraded-search (§3.2, §6.2).

        Members are discovered via the scope fold (the reverse of
        ``graph_neighbors``, which walks a drawer's OWN outgoing scope edges):
        ``fold_scope(kernel).cells_in_scope(scope_ref)``. Room narrows more
        than wing when both are given. Omitting both returns every drawer
        that carries at least one scope edge (best-effort global listing --
        there is no dedicated "all drawers" index).

        Payloads are regenerated with ``record_access=False`` -- this is a
        read surface, not user-facing content access (D4 read-vs-fold
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

        scope_index = fold_scope(s.kernel)  # type: ignore[attr-defined]
        if scope_ref is not None:
            member_refs = scope_index.cells_in_scope(scope_ref)
        else:
            member_refs = set(scope_index.membership.keys())

        out: list[dict[str, Any]] = []
        for ref in sorted(member_refs)[: max(0, limit)]:
            content = s.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            wing_name, room_name = self._resolve_wing_room(ref)
            category = self._first_fact_value(ref, "has_category")
            importance_raw = self._first_fact_value(ref, "has_importance")
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
    ) -> list[dict[str, Any]]:
        """Hybrid rank (§6) or, when ``query_vector`` is None, lexical-only (§6.2).

        ``query_vector=None`` signals the caller's embedder is not ready --
        this method then falls back to a full scoped scan
        (:meth:`list_drawers`) scored purely by
        ``amplifier_module_tool_memory.embedder.lexical_score``. Otherwise:
        cosine top-``k*3`` via ``query_vector`` scoped to room (if given)
        else wing (if given) else global, then
        ``final = 0.85 * cosine + 0.15 * lexical_score`` re-rank, top-``k``.

        Returns ``[{ref, score, content, wing, room, category, source}]`` --
        payloads and facts are regenerated server-side (``record_access=False``).
        The caller (the daemon dispatch layer) is responsible for setting the
        wire-level ``degraded`` flag based on whether it passed a real vector.
        """
        from .embedder import lexical_score

        s = self.store
        scope_ref = None
        if room is not None:
            scope_ref = self._scope_ref("room", room)
        elif wing is not None:
            scope_ref = self._scope_ref("wing", wing)

        scored: list[tuple[Any, float]] = []
        if query_vector is not None:
            candidates = s.query_vector(  # type: ignore[attr-defined]
                list(query_vector), max(1, k * 3), scope=scope_ref
            )
            for ref, cosine in candidates:
                content = s.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                    "utf-8", errors="replace"
                )
                lex = lexical_score(lexical_query or "", content)
                scored.append((ref, 0.85 * cosine + 0.15 * lex))
        else:
            for drawer in self.list_drawers(
                wing=wing, room=room, limit=self._DEGRADED_SEARCH_SCAN_LIMIT
            ):
                scored.append(
                    (
                        drawer["ref"],
                        lexical_score(lexical_query or "", drawer["content"]),
                    )
                )

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        results: list[dict[str, Any]] = []
        for ref, score in scored[: max(0, k)]:
            content = s.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            wing_name, room_name = self._resolve_wing_room(ref)
            category = self._first_fact_value(ref, "has_category")
            source = self._first_fact_value(ref, "has_source")
            results.append(
                {
                    "ref": ref,
                    "score": score,
                    "content": content,
                    "wing": wing_name,
                    "room": room_name,
                    "category": category,
                    "source": source,
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
