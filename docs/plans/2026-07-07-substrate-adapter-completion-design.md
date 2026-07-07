# Substrate Adapter Completion — Design

**Date:** 2026-07-07
**Status:** Implementation-ready design (no code in this change)
**Scope:** Complete the `memory → amplifier-data` consumer adapter in this repo
**Constellation edge:** `memory → amplifier-data` is the ONLY permitted intra-four
edge (AGENTS.md). Nothing here imports survey, CI, or the conductor bundle.

---

## 1. Problem statement

The `AmplifierDataMemoryStore` seam
(`modules/tool-mempalace/amplifier_module_tool_mempalace/scripts/memory_store.py:167-353`)
is real and well past stub: drawers map to `write_cell`, wing/room to `scope()`,
source/category/importance to `assert_fact`, with three backends (direct
`AmplifierStore`, `RemoteStore`, authed `GatewayClient`), a `DualWriteMemoryStore`
fan-out, an opt-in capture-hook shadow (`shadow_gateway`, off by default), and a
§8 migration harness (`dualwrite_compare.py`).

What the seam does NOT yet route (verified against both repos on 2026-07-07):

1. **Embeddings** — palace semantic search stays entirely in ChromaDB; nothing
   flows through `add_embedding` / `query_vector(scope=…)`. The substrate is the
   intended source of truth for vectors (COMPOSITION.md: ChromaDB stays the ANN
   *behind* the `VectorBackend` stud, rebuilt from `iter_embeddings()`).
2. **KG facts** — palace `kg` ops (`__init__.py:258-284` → `mempalace_kg_*` MCP
   calls) and Phase-3 curator facts (`phase3.py`: `has_importance`,
   `has_category`, `duplicates`, `related_to`) never reach the substrate
   temporal lens.
3. **Diary entries** — `diary write` (`__init__.py:294-307`) goes only to the
   palace.
4. **Atomicity** — `memory_store.py:274-285` probes `getattr(s, "update_fact")` /
   `getattr(s, "append_batch")`. **Both probes are stale.** amplifier-data
   shipped the real primitives at pin `c1107b4` and refined them since:
   `AmplifierStore.write_batch() -> WriteBatch` (single-commit atomicity) and
   `store.transaction() -> Transaction` (multi-call compensating rollback), in
   `amplifier_data/envelope.py`. The probe never fires, so `update_importance`
   always takes the degraded sequential invalidate+assert path, and `file()`
   lands a drawer + 2 scopes + up-to-3 facts as 6 separate appends (a crash
   mid-way leaves a partially-scoped drawer).
5. **Read verification** — `dualwrite_compare` proves E1 + scope + facts +
   durability, but nothing proves the substrate can *answer* memory's three read
   shapes (vector search, KG facts/graph, scoped reads) consistently with the
   palace.
6. **Doc truth** — `project-context/PROVENANCE.md:38-47` (D4) still calls the
   seam "a loud `NotImplementedError` stub … blocked on persistence + vector
   lens", and `HANDOFF.md` repeats it. Both are false today.
7. **Dependency pin** — `modules/tool-mempalace/pyproject.toml:31` pins the
   `[substrate]` extra to `c1107b4`, now 7 commits behind amplifier-data main
   (`a99f61f`, Plan 4a intent compiler; Plan 4b convergent integrity landing
   imminently). The pin comment ("main is only docs-ahead") is stale.

This design completes pieces (a)–(f) below by **extending the existing seam
files** — no new architecture, no new modules.

---

## 2. Verified substrate API surface (what we build against)

From `~/dev/amplifier-data` at `a99f61f` (read-only; another agent is
implementing there concurrently — re-verify signatures at implementation time):

| API | Signature | Notes |
| --- | --- | --- |
| `add_embedding` | `(target_ref: Hash, vector: Sequence[float]) -> Hash` | packs LE-f32, writes embedding cell, relates `embedding_of` edge; dim-agnostic (`store.py:188-214`) |
| `query_vector` | `(vector, k, scope: Hash \| None = None) -> list[tuple[Hash, float]]` | brute-force cosine, filter-before-score on scope, D4-clean (`store.py:486-516`) |
| `iter_embeddings` | `() -> list[tuple[Hash, list[float]]]` | rebuild-any-backend seam (`store.py:680`) |
| `write_batch` | `() -> WriteBatch` | stage `write_cell`/`relate`/`assert_fact`/`scope`; `commit()` = ONE atomic `kernel.append_batch`; refs knowable pre-commit (`envelope.py:42-109`) |
| `transaction` | `() -> Transaction` | context manager; compensating rollback on exception (`envelope.py:112-171`) |
| `write_cell` | `(payload, interpreters=(), *, source_interaction_id=None, written_by_hook=None) -> Hash` | causal tags (T3D-3) land atomically with the cell |
| `writes_by_interaction` / `writes_by_hook` | `(str) -> list[Hash]` | causal read-back |
| temporal lens | `assert_fact` / `invalidate_fact` / `query_facts(subject=None, predicate=None)` / `timeline(subject)` | validity by SeqPos; invalidation = `__invalidate__:<pred>` reserved edge (`INVALIDATE_PREFIX` in `amplifier_data.lenses.temporal`) |
| graph lens | `query_graph(start, max_hops, rel_type=None)` / `graph_neighbors(ref, rel_type=None)` | |
| async seam | `write_cell_async` / `relate_async` / `add_embedding_async` / `commit_batch_async` | `asyncio.to_thread` + store write lock |

`RemoteStore` (`amplifier_data/client.py`) mirrors `add_embedding`,
`query_vector`, `query_facts`, `timeline`, `query_graph` — but has **no
`write_batch`**. Our own `GatewayClient` currently exposes only
`write_cell/scope/assert_fact/invalidate_fact/regenerate/graph_neighbors/query_facts`.

**Consequence:** the atomic path is available per-backend, not universally. The
seam must degrade honestly (see §4d).

---

## 3. Design principles applied

- **Extend seam files, don't add architecture.** Every change below lands in a
  file that already exists, except one new DTU-gated e2e test.
- **Palace stays primary.** Every substrate write is shadow/optional; every new
  read surface is verify-only. Policy (when to cut over reads) stays with the
  bundle, out of scope here.
- **Loud failure over silent fallback** for the seam itself (construction
  raises without amplifier-data); **best-effort swallow** only on the
  config-gated shadow paths (existing `_shadow_job` contract, `MemoryStore`
  DualWrite contract).
- **All substrate imports lazy / behind `pytest.importorskip`.** Matches
  existing tests (`test_amplifier_data_store.py`, `test_amplifier_data_gateway.py`,
  `test_shadow_gateway.py`).
- **Don't re-embed.** The palace already computes embeddings (OpenAI
  `text-embedding-3-small`, 1536-dim — verified in
  `modules/hooks-mempalace-interject/.../__init__.py:95-108`, which queries the
  ChromaDB palace collection with that model). The seam accepts vectors; it
  never calls an embedding API itself. The substrate is dim-agnostic, so no
  dimension is hard-coded anywhere in the seam.

---

## 4. Design per piece

### (a) Embeddings / vector routing

**Decision: the seam transports vectors; it does not create them.** Callers
that have an embedding (the verify harness reads them out of ChromaDB; a future
capture path may pass them) hand it to `file()`. This keeps the embedder as
bundle policy per COMPOSITION.md and avoids putting an OpenAI dependency or
API-key requirement inside the storage seam.

**File: `modules/tool-mempalace/amplifier_module_tool_mempalace/scripts/memory_store.py`**

1. Extend the protocol (backward compatible — keyword with default):

```python
class MemoryStore(Protocol):
    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
        embedding: Sequence[float] | None = None,   # NEW
    ) -> None: ...
```

   `RecordingMemoryStore.file` records it; `PalaceMemoryStore.file` **ignores
   it** (the palace embeds internally via ChromaDB — passing one through would
   be a second embedding pipeline); `DualWriteMemoryStore.file` forwards it to
   both.

2. `AmplifierDataMemoryStore.file(...)`: when `embedding` is provided, add it in
   the same write as the drawer (inside the atomic batch when supported, see
   §4d):

```python
if embedding is not None:
    s.add_embedding(ref, embedding)          # sequential path
    # batch path: b.write_cell(_pack_f32(embedding)) + b.relate(emb_ref, ref, EMBEDDING_OF)
```

   For the batch path import `EMBEDDING_OF` lazily from
   `amplifier_data.lenses.vector` and pack with
   `struct.pack(f"<{len(v)}f", *v)` — byte-identical to what `add_embedding`
   does (verified `store.py:211`), so E1/regeneration equivalence holds across
   both paths.

3. New read helper on `AmplifierDataMemoryStore` (verify-only surface):

```python
def search_vectors(
    self, vector: Sequence[float], k: int, *, wing: str | None = None
) -> list[tuple[str, float]]:
    """Top-k cosine over shadowed embeddings, optionally scoped to a wing.

    Scope ref is recomputed via write_cell(f"wing:{wing}") — content
    addressing makes this idempotent (no duplicate cell, same ref).
    """
```

   Implementation: `scope = s.write_cell(f"wing:{wing}".encode()) if wing else None`
   then `s.query_vector(vector, k, scope=scope)`.

**File: `.../scripts/amplifier_data_gateway.py`** — gateway + client parity:

- `_dispatch` gains tools (each under the existing lock for writes):
  - `add_embedding`: `{"target_ref", "vector": [float, ...]}` → `{"ref"}`
  - `query_vector`: `{"vector", "k", "scope": str|None}` → `{"results": [[ref, score], ...]}` (read; consistent with existing read dispatch)
- `GatewayClient` gains matching methods:

```python
def add_embedding(self, target_ref: str, vector: Sequence[float]) -> str: ...
def query_vector(
    self, vector: Sequence[float], k: int, scope: str | None = None
) -> list[tuple[str, float]]: ...
```

**Encoding facts (recorded here as the contract):**
- Vector payload: little-endian f32, packed by the substrate (`add_embedding`)
  or byte-identically by the seam's batch path. Never JSON floats.
- Dimensions: whatever the palace embedder produces — 1536 today
  (`text-embedding-3-small`). The seam asserts only `len(vector) > 0`; it does
  NOT validate a fixed dimension (the substrate is dim-agnostic; a dim change is
  an embedder-policy event, not a seam event). The verify harness (§4e) DOES
  assert dimensional consistency within one comparison run.
- Scope cells: unchanged convention — `wing:{wing}` / `room:{room}` UTF-8 cells.

### (b) KG facts → temporal lens

**The encoding problem:** palace KG entities are strings
(`mempalace_kg_add(subject, predicate, object)`), but substrate facts are
`(Hash, str, Hash)`. **Decision: anchor cells.** A string entity maps to a
content-addressed cell `entity:{name}` (UTF-8). Content addressing makes the
mapping deterministic, idempotent, and collision-free against the existing
`wing:`/`room:` scope cells and bare value cells.

**File: `memory_store.py` — new methods on `AmplifierDataMemoryStore`:**

```python
def _anchor(self, name: str) -> Any:  # Hash
    """Content-addressed anchor cell for a string KG entity ('entity:{name}')."""
    return self.store.write_cell(f"entity:{name}".encode("utf-8"))

def assert_kg(self, subject: str, predicate: str, object: str) -> None:
    """Palace-shaped KG assert: strings in, anchor-cell fact in the substrate."""
    s = self.store
    s.assert_fact(self._anchor(subject), predicate, self._anchor(object))

def invalidate_kg(self, subject: str, predicate: str, object: str) -> None:
    s = self.store
    s.invalidate_fact(self._anchor(subject), predicate, self._anchor(object))

def query_kg(
    self, subject: str | None = None, predicate: str | None = None
) -> list[tuple[str, str, str]]:
    """Currently-valid facts; anchor refs resolved back to entity strings
    via regenerate(record_access=False). Verify-only read surface."""

def kg_timeline(self, subject: str) -> list[dict[str, Any]]:
    """SeqPos-ordered assert/invalidate history for one entity
    (wraps store.timeline(self._anchor(subject)))."""
```

Note: `query_kg` resolving objects requires `regenerate` — the GatewayClient
already exposes it; `record_access=False` keeps it D4-clean on direct stores.

**Curator Phase-3 facts** (`phase3.py` emits
`KGFact(subject="drawer:<id>", predicate, object)` with predicates
`has_importance` / `has_category` / `duplicates` / `related_to`): these are
already palace-shaped string triples, so they route through the SAME
`assert_kg` surface — no special-casing. `duplicates`/`related_to` become
anchor↔anchor facts (drawer entity to drawer entity), traversable via
`graph_neighbors(anchor, rel_type="duplicates")`. `has_importance`/
`has_category` become anchor→value-anchor facts. Phase 3 itself stays pure
(`phase3.py` is untouched); whoever applies `plan_phase3_actions` output gains
a shadow branch (see wiring below).

**Wiring point — `modules/tool-mempalace/amplifier_module_tool_mempalace/__init__.py`:**
the `kg` operation (`execute()`, lines 258-284). After each successful palace
`_mcp_call` for `kg_action in ("add", "invalidate")`, best-effort shadow:

```python
_shadow_kg(kg_action, subject, predicate, object)   # module-level helper
```

`_shadow_kg` mirrors the capture hook's `_shadow_job` contract exactly: no-op
when unconfigured, never raises, emits `kg_shadow_filed` / `kg_shadow_failed`
events when `emit_events` is on. Configuration: the tool's `mount()` reads an
optional `shadow_gateway: {enabled, base_url, token_file}` block from its
config (same schema as the capture hook's — one knob vocabulary across the
bundle) and constructs a module-level
`_SHADOW_STORE: AmplifierDataMemoryStore | None` via `GatewayClient`. Off by
default; documented in `behaviors/mempalace.yaml` under the `tool-mempalace`
config block (commented out, mirroring `behaviors/mempalace.yaml:47-50`).

### (c) Diary entries → cells

**File: `memory_store.py` — new method:**

```python
def file_diary(
    self, *, agent_name: str, entry: str, topic: str = "general"
) -> Any:  # Hash
    """Diary entry as a cell, scoped to the agent and the topic.

    Scope cells: 'agent:{agent_name}' (per-agent scope — the requirement)
    and 'room:{topic}' (reuses the existing room convention).
    A 'has_source' fact marks provenance ('diary:{agent_name}').
    """
```

Implementation is `file()`-shaped: `write_cell(entry)` + `scope(ref,
write_cell(f"agent:{agent_name}"))` + `scope(ref, write_cell(f"room:{topic}"))`
+ `assert_fact(ref, "has_source", write_cell(f"diary:{agent_name}".encode()))` —
atomic batch when supported (§4d). The `agent:` scope prefix is new and
deliberate: agent diaries are a scope axis orthogonal to wings.

**Wiring point — `__init__.py` `diary` op (lines 294-307):** after a successful
`mempalace_diary_write`, best-effort `_shadow_diary(agent_name, entry, topic)`
using the same `_SHADOW_STORE` as (b). Same never-raise contract, same events
(`diary_shadow_filed` / `diary_shadow_failed`).

### (d) Atomic batch adoption (the probe fix)

**File: `memory_store.py`.**

1. **Replace the stale probe** (`:274-285`):

```python
def _supports_atomic_update(self) -> bool:
    """True iff the backend exposes the WriteBatch atomic primitive.

    Direct AmplifierStore: yes (envelope.WriteBatch, shipped at c1107b4).
    GatewayClient: yes (the gateway 'batch' tool, this change).
    RemoteStore: NO — the companion server has no batch endpoint; the seam
    degrades to the sequential path and records atomic=False honestly.
    """
    return callable(getattr(self.store, "write_batch", None))
```

   Delete the `update_fact` probe entirely — no such API exists or is planned;
   the docstring claim "this is the Step-3 / T3D-2 addition owned by the
   amplifier-data repo" is now delivered under a different name
   (`write_batch`).

2. **`update_importance` atomic path** (replaces `:327-337`):

```python
if self._supports_atomic_update():
    from amplifier_data.lenses.temporal import INVALIDATE_PREFIX  # lazy; gateway path uses its own constant
    b = s.write_batch()
    if old_ref is not None:
        b.relate(subject, old_ref, INVALIDATE_PREFIX + "has_importance")
    b.assert_fact(subject, "has_importance", new_ref)
    b.commit()          # ONE kernel.append_batch — all-or-nothing
else:
    ...existing sequential path, unchanged, atomic=False on the record...
```

   `WriteBatch` has no `invalidate_fact` sugar (verified `envelope.py:75-91`);
   the `__invalidate__:`-prefixed relate IS the documented reserved-type
   convention (CONSUMER_INTEGRATION §2). Import the constant, don't inline the
   string. For the GatewayClient path the constant lives client-side in
   `amplifier_data_gateway.py` (`_INVALIDATE_PREFIX = "__invalidate__:"`) since
   the client must stay import-free of amplifier-data.

3. **`file()` becomes atomic when supported:** drawer cell + `wing:`/`room:`
   scopes + `has_source`/`has_category`/`has_importance` facts + optional
   embedding (cell + `embedding_of` edge) staged on one `WriteBatch`, one
   `commit()`. Refs are knowable pre-commit (content addressing), so the staged
   graph wires exactly as the sequential path does. Sequential fallback remains
   for `RemoteStore` — behavior-identical end state, just not crash-atomic.

4. **Gateway batch support** (`amplifier_data_gateway.py`):
   - New dispatch tool `batch`: `{"ops": [{"op": "write_cell", "payload_b64": …} |
     {"op": "relate"|"assert_fact"|"scope", …refs…}]}`. The server materializes a
     real `store.write_batch()`, stages every op, commits under the existing
     lock, and returns `{"refs": [...]}` for the staged `write_cell` ops. One
     HTTP call = one atomic commit.
   - `GatewayClient.write_batch()` returns a small `GatewayWriteBatch` shim with
     the same `write_cell/relate/assert_fact/scope/commit` surface. Computing
     refs client-side would replicate the substrate's addressing — **don't**:
     the shim's `write_cell(payload)` returns an opaque `_PendingRef` token,
     resolvable after `commit()`; internal wiring between staged ops uses the
     tokens. This keeps the client stdlib-only and makes zero assumptions
     about addressing. If this proves awkward in implementation, the acceptable
     simplification is domain-shaped composite tools (`batch_file`,
     `batch_update_importance`) built server-side — prefer whichever is
     smaller; the killer gate (KG-A1) is backend-agnostic.

5. **`Transaction` adoption is deliberately deferred.** `MutationRecord` +
   `rollback()` already provide multi-call recovery at the seam, and every
   multi-write flow we have fits in one `WriteBatch`. Adopting `Transaction`
   would duplicate the rollback concern in two layers. Revisit only when a flow
   genuinely needs cross-commit rollback. (Recorded as a decision, not a gap.)

### (e) Read-path shadow-verify (NOT a read migration)

**File: `dualwrite_compare.py` — extend `run_compare` and the report.** The
palace remains the only production read source; this harness proves the
substrate COULD answer, by comparison.

New checks appended to the existing E1/scope/facts/durability loop:

1. **Vector**: for each filed cell that has an embedding, `query_vector(vec,
   k=1, scope=wing_ref)` must return that cell's ref top-1 (self-retrieval —
   exact, deterministic, no recall ambiguity). Report fields:
   `vector_top1_ok`, `vector_scoped_total`.
   Embedding source, in order: (i) `--content-file` entries may carry an
   `"embedding": [...]` list (real palace vectors exported from ChromaDB via
   `collection.get(include=["embeddings"])`); (ii) otherwise the harness
   generates deterministic synthetic unit vectors (seeded per-content hash) —
   clearly labelled `embedding_source: "synthetic"` in the report. Synthetic
   vectors still prove the routing/scoping/regeneration machinery; real ones
   add fidelity.
2. **KG**: through the seam's new `assert_kg`/`invalidate_kg`/`query_kg`/
   `kg_timeline`: assert a fact per categorized cell
   (`drawer:<n>, has_category, <cat>`), verify `query_kg` sees it, invalidate
   one, verify it disappears from `query_kg` but appears in `kg_timeline` with
   both entries (validity window). Report: `kg_assert_ok`, `kg_invalidate_ok`,
   `kg_timeline_ok`.
3. **Scope queries**: for each wing used, count cells returned by scoped
   graph/vector reads vs. the palace-mirror's records for that wing. Report:
   `scope_query_consistent`.
4. **Diary**: file one diary entry via `file_diary`, read it back via the
   `agent:` scope, byte-compare. Report: `diary_ok`.

The `PASS`/`FAIL` gate at `main()` extends to require all new fields green.
New CLI flag: `--embeddings-file` (optional JSON `{content_sha: [floats]}`) to
attach real palace vectors to `--content-file` corpora. Nothing writes to the
real palace; the shadow store stays a throwaway `tempfile` path, exactly as
today.

### (f) Doc truth

- **`project-context/PROVENANCE.md`** — PROVENANCE is a decision *log*: do not
  rewrite D4 (lines 38-47); append a new dated entry
  `### D5 — Substrate seam completed (supersedes D4's "stub" status)` recording:
  seam status (3 backends, shadow, harness), the fork resolution implied by
  this work (amplifier-data persistence + vector lens DID land; D4's "open fork
  for Michael" resolved toward composition), the anchor-cell KG encoding, the
  `agent:` scope prefix, the WriteBatch probe decision, and the deliberate
  deferral of `Transaction` + read cutover. Add one line under D4 itself:
  *"(Superseded by D5, 2026-07-XX — the stub described here was completed.)"*
- **`project-context/HANDOFF.md`** — per AGENTS.md this is updated at session
  end anyway; the implementing session must replace the stale seam-status
  claims ("deliberate loud NotImplementedError stub", "blocked on persistence
  + vector lens", the substrate-fork "Blocked/Unresolved" bullet) with current
  truth and the new next-steps (read-cutover policy decision, ANN-behind-
  VectorBackend follow-on).

### Pin bump (cross-cutting)

**File: `modules/tool-mempalace/pyproject.toml:23-32`.**

- Bump `amplifier-data @ git+…@c1107b4…` to the **amplifier-data main HEAD SHA
  at implementation time** — `a99f61f` as of this design, but Plan 4b
  (convergent integrity) is landing imminently; the implementer MUST take
  `git -C ~/dev/amplifier-data fetch && git rev-parse origin/main` at the
  moment of the change, and re-run the seam test suite against it.
- Rewrite the stale pin comment: `c1107b4` / "main is only docs-ahead" is no
  longer true (7+ functional commits ahead: Plan 4a intent compiler, autonomic
  indexing, lint fixes). Keep the two load-bearing parts of the comment:
  (i) full SHA, never `@main`, for reproducibility; (ii) **PIN COUPLING** —
  the same SHA is mirrored in the conductor bundle at
  `amplifier-bundle-behavioral-plasticity/modules/dep-amplifier-data/pyproject.toml`
  `[project.dependencies]` and both MUST move in one atomic change. This design
  does not edit the conductor repo (out of scope / not this repo), but the
  implementing session must flag the coupled bump loudly in HANDOFF.md and the
  commit message so the conductor-side change happens in lockstep.

---

## 5. Killer gates (numbered, executable)

Each gate is a concrete assertion an implementer encodes as a test (module
placement per §6). All substrate gates begin with
`pytest.importorskip("amplifier_data")`.

| Gate | Assertion |
| --- | --- |
| **KG-V1** | Drawer filed via `AmplifierDataMemoryStore.file(..., embedding=v)` with wing `w` → `search_vectors(v, k=1, wing=w)` returns `(ref, ~1.0)` top-1; the same query scoped to a *different* wing returns `[]`. |
| **KG-V2** | The embedding cell regenerates byte-identically as LE-f32 (`struct.unpack` round-trip equals input within f32 precision) after `close()` + reopen of a durable store; `iter_embeddings()` contains `(ref, v)`. |
| **KG-A1** | `update_importance` on a `write_batch`-capable backend is atomic: (i) monkeypatched `kernel.append_batch` is called exactly ONCE for the whole update; (ii) crash-injection — force `append_batch` to raise → `query_facts(subject, "has_importance")` still returns the OLD value (no invalidated-but-not-reasserted half-state); (iii) the returned `MutationRecord.atomic is True`. On a `RemoteStore`-shaped stub (no `write_batch`), `atomic is False` and the sequential path still lands the update. |
| **KG-A2** | `file()` with category+importance+embedding on a batch-capable backend produces exactly ONE `append_batch` containing cell + 2 scope edges + facts + embedding events; end state is lens-identical to the sequential path (same `query_facts`, `graph_neighbors`, `query_vector` answers). |
| **KG-K1** | `assert_kg("svc-a", "depends_on", "svc-b")` → `query_kg(subject="svc-a")` contains the triple; `invalidate_kg(...)` → gone from `query_kg` but `kg_timeline("svc-a")` shows assert-then-invalidate (validity window, SeqPos-ordered). Shadow wiring: the tool's `kg add` op with `_SHADOW_STORE` configured against a live gateway lands the same fact substrate-side; with a dead gateway URL, the palace op still succeeds and `kg_shadow_failed` is emitted (never raises). |
| **KG-K2** | Phase-3-shaped facts round-trip: `assert_kg("drawer:1", "duplicates", "drawer:2")` → `graph_neighbors(anchor("drawer:1"), rel_type="duplicates") == [anchor("drawer:2")]`; `has_importance`/`has_category` value facts resolve back to their strings via `query_kg`. |
| **KG-D1** | `file_diary(agent_name="curator", entry=e, topic="t")` → the cell is reachable via the `agent:curator` scope (graph neighbors of ref include the agent scope cell), regenerates byte-identical to `e`, and carries `has_source = diary:curator`. |
| **KG-R1** | `mempalace-dualwrite-compare` (extended) exits 0 with ALL new report fields green (`vector_top1_ok == vector_scoped_total`, `kg_assert_ok`, `kg_invalidate_ok`, `kg_timeline_ok`, `scope_query_consistent`, `diary_ok`) on the representative corpus, AND after durable close+reopen. |
| **KG-P1** | `pip install -e 'modules/tool-mempalace[substrate]'` in a fresh venv resolves the NEW SHA (assert via `importlib.metadata` direct-URL info ending with the pinned SHA); on the installed package, `callable(AmplifierStore().write_batch)` is true → the probe reports atomic on the direct backend. |
| **KG-G1** | Gateway parity: new tools (`add_embedding`, `query_vector`, `batch`) round-trip through `GatewayClient` against a live in-memory gateway — authed remote path (bad token → 401) and localhost bypass both covered; a batch through the gateway is atomic per KG-A1(i) semantics server-side. |

---

## 6. Test plan

Per repo convention (PROVENANCE "Process note", HANDOFF "Non-Obvious Context"):
unit tests live under `modules/*/tests/` (repo-root `tests/` is DTU-gated);
run per-module; substrate tests gated by `pytest.importorskip("amplifier_data")`.

| File | Covers | New/extend |
| --- | --- | --- |
| `modules/tool-mempalace/tests/test_amplifier_data_store.py` | KG-V1, KG-V2, KG-A1, KG-A2, KG-K1 (seam half), KG-K2, KG-D1 | extend |
| `modules/tool-mempalace/tests/test_amplifier_data_gateway.py` | KG-G1 | extend |
| `modules/tool-mempalace/tests/test_dualwrite_compare.py` | KG-R1 (invoke `run_compare` in-process on the representative corpus; assert report fields) | new file, same importorskip pattern |
| `modules/tool-mempalace/tests/test_substrate_pin.py` | KG-P1 (skip unless amplifier_data installed; assert direct-URL SHA + probe) | new, tiny |
| `modules/tool-mempalace/tests/test_tool_shadow_ops.py` | KG-K1 (tool-wiring half) + diary shadow: `_shadow_kg`/`_shadow_diary` no-op unconfigured, land via live gateway, swallow dead-URL failures — mirrors `test_shadow_gateway.py` structure | new |
| `modules/hooks-mempalace-capture/tests/test_shadow_gateway.py` | unchanged behavior regression (protocol gained a kwarg — existing tests must stay green) | verify only |
| `tests/integration/test_substrate_shadow_e2e.py` | DTU smoke (§7) | new, DTU-gated |

Non-substrate environments: every new test skips cleanly (importorskip), so
per-module CI (`.github/workflows/contract.yml`, which installs no substrate
extra) stays green by construction. Run book:

```
cd ~/dev/amplifier-bundle-memory
../.venv/bin/python -m pytest modules/tool-mempalace/tests -q
../.venv/bin/python -m pytest modules/hooks-mempalace-capture/tests -q
```

plus one run in a venv with `.[substrate]` installed so the gates actually
execute (this is the run that counts; a skipped gate is not a passed gate).

## 7. DTU e2e spec

**Profile: `.amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml`**
(the load profile is untouched). Additions to `provision.setup_cmds`, after
step 6 (bundle clone) and before the Amplifier install:

1. Install the seam with the substrate extra (pure-Python fallback — no Rust
   toolchain in the container; durable `path=` is therefore NOT used
   in-container, the gateway runs an in-memory store, which is sufficient for
   the smoke — durability is already gated in unit tests where maturin exists):
   `pip install --break-system-packages -e '/workspace/amplifier-bundle-memory/modules/tool-mempalace[substrate]'`
2. Start the gateway as a background service and capture its discovery line:
   `nohup mempalace-amplifier-data-gateway --port 8799 > /root/gateway.json.log 2>&1 &`
3. Enable the shadow in the behavior config used by the container run: set
   `shadow_gateway: {enabled: true, base_url: "http://127.0.0.1:8799"}` on
   BOTH `hooks-mempalace-capture` and `tool-mempalace` config blocks (sed-patch
   the cloned `behaviors/mempalace.yaml`; the committed default stays OFF).

**Smoke test `tests/integration/test_substrate_shadow_e2e.py`** (DTU-gated like
every root test — skipped outside the container), exercising the completed seam
end-to-end as a user would:

1. **File a drawer through the shadow**: invoke the capture path (or
   `mempalace mcp --call mempalace_add_drawer` + the `_shadow_job`-shaped
   call) with known content, wing `wing_e2e`, category `decision`.
2. **Attach an embedding + facts through the gateway**: `GatewayClient` →
   `add_embedding(ref, v)` (deterministic test vector) and `assert_fact(ref,
   "has_category", …)`; a `kg add` via the tool op for the anchor-cell path.
3. **Read it all back from the substrate** (palace untouched as read primary):
   `regenerate(ref)` byte-equals the content; `graph_neighbors(ref,
   "scoped_to")` contains `wing:wing_e2e`; `query_vector(v, 1, scope=wing_ref)`
   returns `ref` top-1; `query_facts(subject=ref)` shows the category fact;
   the tool-shadowed KG triple is visible via `query_facts`.
4. **Assert the shadow never harmed the primary**: the palace search
   (`mempalace mcp --call mempalace_search`) still returns the drawer, and the
   session event log contains `shadow_filed` (not `shadow_failed`).

Pass = all four stages green in one container run after `reset-palace`.

## 8. Definition of Done

- [ ] `memory_store.py`: protocol `embedding` kwarg; `file()` embedding + batch path; `search_vectors`; `_anchor`/`assert_kg`/`invalidate_kg`/`query_kg`/`kg_timeline`; `file_diary`; probe rewritten to `write_batch`; `update_importance` atomic path; stale docstrings corrected (module header lines 1-12 still say "fails loudly until persistence + a vector lens land").
- [ ] `amplifier_data_gateway.py`: `add_embedding` / `query_vector` / `batch` tools + `GatewayClient` methods; `_INVALIDATE_PREFIX` client constant.
- [ ] `__init__.py` (tool-mempalace): shadow config in `mount()`; `_shadow_kg` + `_shadow_diary` wired into `kg` and `diary` ops; events emitted.
- [ ] `dualwrite_compare.py`: vector / KG / scope / diary verify checks, report fields, `--embeddings-file`, extended PASS gate.
- [ ] `behaviors/mempalace.yaml`: commented `shadow_gateway` block documented on `tool-mempalace` config.
- [ ] `pyproject.toml`: pin bumped to current amplifier-data main SHA; comment rewritten; conductor-coupling flagged in commit + HANDOFF (conductor repo bumped in lockstep by its owner).
- [ ] All killer gates KG-V1…KG-G1 encoded as tests and **executed green in a substrate-installed venv** (not merely skipped).
- [ ] Existing suites regression-green: `modules/tool-mempalace/tests`, `modules/hooks-mempalace-capture/tests`, `modules/hooks-mempalace-briefing/tests`.
- [ ] `python_check` clean (no new errors; the intentional-stub warning for the seam disappears).
- [ ] DTU: e2e profile updated + `test_substrate_shadow_e2e.py` passing in the container (or, if a container run is not feasible this session, the profile+test land and HANDOFF says so explicitly — never claim an unrun smoke).
- [ ] `PROVENANCE.md` D5 appended + D4 superseded note; `HANDOFF.md` seam-status truth restored.
- [ ] No edit to `~/dev/amplifier-data` (read-only peer; concurrent implementation in flight there).

## 9. Explicitly out of scope

- Read-path **cutover** (palace remains the production read source; §4e is verification only — cutover is a policy decision for a future design).
- `Transaction` adoption (deferred, §4d.5 — `MutationRecord` rollback already covers multi-call recovery).
- ChromaDB-behind-`VectorBackend` as a substrate accelerator (the composition target per COMPOSITION.md, but it belongs to the read-cutover phase; this change only makes the substrate the *write* target for vectors).
- Re-embedding / mutable-embedding wiring (`embeddings.py` T1-MEM-4 stays OFF and unwired).
- Conductor-repo pin edit (coupled, flagged, owned by the conductor bundle).
