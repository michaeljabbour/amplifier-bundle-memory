"""
MemoryStore — the storage seam for the consolidation pipeline.

The cold-path ``curate.dot`` pipeline produces consolidated "cells" and writes
them through a ``MemoryStore``. The MemPalace drawer store (``PalaceMemoryStore``)
is the production read source; the amplifier-data substrate
(``AmplifierDataMemoryStore``) is a completed consumer seam — drawers, scopes,
KG facts, vectors, and diary entries all route through it, with three
interchangeable backends (direct ``AmplifierStore``, ``RemoteStore`` via the
companion server, and the authed ``GatewayClient``). It is written as a
shadow/verify surface today (docs/plans/2026-07-07-substrate-adapter-completion-design.md);
read-path cutover is a deliberate follow-on policy decision, out of scope here.

Keeping ``file()`` as the common protocol method means the pipeline's
``write_cells`` node is identical regardless of backend; only the injected
store changes. The additional seam surfaces (``search_vectors``, ``assert_kg``
/ ``query_kg`` / ``kg_timeline``, ``file_diary``) are verify-only reads and
additive writes layered on top — they do not change the core ``file()`` contract.
"""

from __future__ import annotations

import json
import struct
import subprocess
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from amplifier_module_tool_mempalace.scripts.mutation import (
    MutationRecord,
    ReversibleDelta,
    new_mutation,
)


#: JSON-RPC protocol version negotiated with ``mempalace-mcp``. Any value in
#: mempalace's ``SUPPORTED_PROTOCOL_VERSIONS`` works; this is simply the
#: newest one as of mempalace 3.5.0.
_MCP_PROTOCOL_VERSION = "2025-06-18"


def _call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke one palace MCP tool via a fresh ``mempalace-mcp`` stdio session.

    mempalace 3.5.0 has NO synchronous single-shot CLI call surface.
    Verified against the installed package (mempalace 3.5.0, the latest on
    PyPI, source inspected in site-packages): ``mempalace mcp``
    (mempalace/cli.py:cmd_mcp) only PRINTS the shell command to wire
    MemPalace into an MCP host and exits -- it never accepts ``--call``, and
    its argparse subparser declares only ``--backend``. The real MCP server
    is a *separate* console script, ``mempalace-mcp`` (mempalace/mcp_server.py),
    which speaks newline-delimited JSON-RPC 2.0 over stdio: an
    ``initialize`` handshake followed by ``tools/call`` requests, one JSON
    response per line (mempalace/mcp_server.py:_run_stdio_loop).

    This sends both messages up front via a single ``subprocess.run(input=...)``
    call so the server's normal EOF-on-closed-stdin shutdown
    (``_run_stdio_loop``'s ``if not line: break``) exits the process cleanly --
    no separate terminate/kill step needed. A fresh process is spawned per
    call, mirroring the one-shot-per-call design the previous (nonexistent)
    ``mempalace mcp --call`` invocation assumed.

    Args:
        env: Optional environment override passed straight through to
            ``subprocess.run``. ``None`` (default) inherits the current
            process environment, identical to omitting the kwarg entirely.
            Callers that need to target a specific palace directory (e.g.
            test fixtures pointing ``MEMPALACE_DIR`` at a temp dir) pass a
            full env dict here rather than mutating ``os.environ``.

    Returns the tool's own result payload, unwrapped from the MCP
    ``result.content[0].text`` JSON envelope, or ``{"error": "..."}`` on any
    transport or JSON-RPC-level failure -- the same shape callers already
    check via ``result.get("error")``.
    """
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": _MCP_PROTOCOL_VERSION, "capabilities": {}},
    }
    call_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    stdin_payload = json.dumps(init_req) + "\n" + json.dumps(call_req) + "\n"

    try:
        proc = subprocess.run(
            ["mempalace-mcp"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    call_response: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            call_response = msg
            break

    if call_response is None:
        return {
            "error": (
                f"mempalace-mcp produced no tools/call response for "
                f"{tool_name!r} (rc={proc.returncode}). "
                f"stderr: {proc.stderr.strip()[:500]}"
            )
        }
    if "error" in call_response:
        rpc_error = call_response["error"]
        message = rpc_error.get("message") if isinstance(rpc_error, dict) else rpc_error
        return {"error": message or str(rpc_error)}

    content = (call_response.get("result") or {}).get("content") or []
    text_out = content[0].get("text") if content else None
    if text_out is None:
        return {"error": "tools/call result missing content[0].text"}
    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        return {"raw": text_out}


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


class PalaceMemoryStore:
    """Writes consolidated cells as verbatim MemPalace drawers via the MCP tool surface.

    Mirrors the capture hook's ``_mcp_add_drawer`` call so consolidated content
    lands in the palace exactly like raw captures do -- both route through
    ``_call_mcp_tool`` (see its docstring for why the previous
    ``mempalace mcp --call`` invocation never worked against real mempalace).
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
        embedding: Sequence[float] | None = None,
    ) -> None:
        # embedding is intentionally IGNORED: the palace embeds internally via
        # ChromaDB. Passing a vector through here would stand up a second,
        # divergent embedding pipeline for the same content.
        result = _call_mcp_tool(
            "mempalace_add_drawer",
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source_file": source,
                "added_by": self.added_by,
            },
        )
        if result.get("error"):
            # The palace is the primary/source-of-truth store -- a failed
            # write here must be loud, not silently discarded (the previous
            # subprocess.run(...) never checked returncode or output at all,
            # which is how this call site's total breakage went unnoticed).
            raise RuntimeError(f"PalaceMemoryStore.file failed: {result['error']}")


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

        Palace KG entities are strings; substrate facts are ``(Hash, str, Hash)``.
        Content addressing makes this mapping deterministic, idempotent, and
        collision-free against the existing ``wing:``/``room:`` scope cells.
        """
        return self.store.write_cell(f"entity:{name}".encode("utf-8"))  # type: ignore[attr-defined]

    def assert_kg(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        """Palace-shaped KG assert: strings in, anchor-cell fact in the substrate."""
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
        embedding: Sequence[float] | None = None,
    ) -> None:
        self.primary.file(
            wing=wing,
            room=room,
            content=content,
            source=source,
            category=category,
            importance=importance,
            embedding=embedding,
        )
        try:
            self.shadow.file(
                wing=wing,
                room=room,
                content=content,
                source=source,
                category=category,
                importance=importance,
                embedding=embedding,
            )
        except Exception as exc:  # shadow must never break the source of truth
            self.shadow_errors.append(f"{type(exc).__name__}: {exc}")
            if self.fail_on_shadow_error:
                raise
