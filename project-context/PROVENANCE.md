# Provenance — Decision Log

## 2026-06-05 — Memory architecture Phase 1 + 2 (autonomous weekend build)

Branch `feat/manifest-and-curate-pipeline`. Decisions made autonomously per
Michael's instruction to "build phase 1 and 2 over the weekend… get it done."
These are defaults; flagged for review.

### D1 — Manifest scope: per-project with fallback chain
**Decision:** `load_manifest` resolves in order: explicit `manifest_path` config →
`<project>/project-context/memory-manifest.yaml` → `~/.amplifier/memory-manifest.yaml`
→ in-code `DEFAULT_MANIFEST`.
**Why:** Per-project is the most useful (different repos accumulate different
knowledge) but must never be mandatory. The in-code default mirrors the legacy
hardcoded table exactly, so existing behavior is preserved with zero config.
**Alternatives:** global-only (rejected: not steerable per project); file-required
(rejected: breaks no-config deployments).

### D2 — Cold-path trigger: on-demand only (Phase 2)
**Decision:** curate.dot runs on-demand via the Curator ("consolidate my memory").
No automatic session:end trigger yet.
**Why:** foundation-expert guidance (2026-06-05): Option C is the right MVP —
zero hot-path risk, works whether attractor is installed or not. A volume-gated
session:end hook is the right follow-on but its optionality must be a Python
capability check (`coordinator.get_tool("run_pipeline") is None → no-op`), never
a hard YAML dependency. Each box node spawns a child LLM session, so automatic
triggering has real cost and must be opt-in + gated.
**Alternatives:** session:end hook now (deferred — cost + optionality complexity);
compose attractor in behaviors/mempalace.yaml (rejected: hard dependency).

### D3 — Emergent policy: declared, default off
**Decision:** `emergent.enabled` (default false) + `promote_threshold` live in the
manifest schema. The pipeline may propose emergent categories; the user confirms.
Not yet wired into classification logic.
**Why:** Keep the precision/recall knob visible and user-owned, but don't ship
auto-promotion behavior unproven. "Propose, don't drop, don't silently park."

### D4 — Substrate: palace now, amplifier-data as a declared seam
**Decision:** Phase 2 writes through `PalaceMemoryStore` (ChromaDB palace).
`AmplifierDataMemoryStore` exists as a loud `NotImplementedError` stub.
**Why:** amplifier-data's Rust floor is proven (E1–E3 green) but it lacks
persistence + a vector lens — the two things memory actually needs. Writing
through a `MemoryStore` seam means swapping to amplifier-data later is a
one-class change, with the gap explicit rather than silent.
**Open fork for Michael:** commit to amplifier-data (fund persistence + vector
lens) vs. stay on the palace and keep amplifier-data concept-only. Phase 3 gated
on this.
**(Superseded by D5, 2026-07-07 — the stub described here was completed.)**

### D5 — Substrate seam completed (supersedes D4's "stub" status)
**Date:** 2026-07-07. Implements
`docs/plans/2026-07-07-substrate-adapter-completion-design.md`.

**Fork resolution:** D4's "open fork" (commit to amplifier-data vs. stay
palace-only) resolved toward **composition**: amplifier-data's persistence
(durable `DurableKernel`/Rust) and vector lens (dim-agnostic `query_vector`/
`add_embedding`) DID land upstream (verified against amplifier-data at HEAD
`09482f1`). The seam is no longer a stub — it is a completed consumer
adapter with three interchangeable backends (direct `AmplifierStore`,
`RemoteStore` via the companion server, authed `GatewayClient`), a
`DualWriteMemoryStore` fan-out, an opt-in capture-hook AND tool-op shadow, and
a §8 migration/read-verify harness (`dualwrite_compare.py`).

**Seam status (what now routes through amplifier-data):**
- **Drawers** — `write_cell` + `wing:`/`room:` scope cells + source/category/
  importance facts (unchanged from D4, now atomic — see below).
- **Embeddings** — `file(..., embedding=v)` transports a caller-supplied
  vector to `add_embedding`/the batch path; `search_vectors(v, k, wing=...)`
  is the verify-only read. The seam NEVER computes embeddings itself (bundle
  policy per COMPOSITION.md stays with the embedder, e.g. ChromaDB's
  `text-embedding-3-small`); it is dim-agnostic by construction.
- **KG facts** — anchor-cell encoding: a palace string entity (e.g.
  `"svc-a"`) maps to a content-addressed `entity:{name}` cell so
  `assert_kg`/`invalidate_kg`/`query_kg`/`kg_timeline` can carry palace-shaped
  string triples onto substrate `(Hash, str, Hash)` facts. Serves both the
  `tool-mempalace` `kg` op (shadowed best-effort via `_shadow_kg`) and
  Phase-3 curator facts (`has_importance`/`has_category`/`duplicates`/
  `related_to`) — no special-casing, same `assert_kg` surface.
- **Diary** — `file_diary(agent_name, entry, topic)` introduces a NEW scope
  axis, `agent:{agent_name}`, orthogonal to `wing:`/`room:`. Shadowed
  best-effort from the palace `diary write` op via `_shadow_diary`.

**The `write_batch` probe decision:** the old probe
(`getattr(s, "update_fact")` / `getattr(s, "append_batch")`) never fired —
amplifier-data shipped the real primitive under a different name,
`AmplifierStore.write_batch() -> WriteBatch` (envelope.py). The probe is
rewritten to `callable(getattr(self.store, "write_batch", None))`. On a
capable backend (direct `AmplifierStore`, or `GatewayClient` via the new
gateway `batch` tool), `file()`/`file_diary()`/`update_importance()` stage
their writes on ONE `WriteBatch` and commit as ONE atomic `append_batch` — a
crash mid-way leaves NO half-written state. `RemoteStore` has no batch
endpoint and degrades honestly: sequential path, `MutationRecord.atomic=False`.

**Deliberately deferred (recorded as decisions, not gaps):**
- **`Transaction` adoption** — `MutationRecord` + `rollback()` already cover
  multi-call recovery, and every multi-write flow in this repo fits in one
  `WriteBatch`. Revisit only when a flow genuinely needs cross-commit
  rollback.
- **Read-path cutover** — the palace remains the ONLY production read
  source. The new `dualwrite_compare.py` checks (vector self-retrieval, KG
  assert/invalidate/timeline, scope-query consistency, diary round-trip) are
  verification that the substrate COULD answer, not a migration. Cutover
  policy is a future design decision.

### Process note — test placement
Discovered that repo-root `tests/` is DTU-gated (skipped outside the
memory-bundle-e2e container). Unit tests for this work were placed under
`modules/*/tests/` instead, with a local conftest for the capture-hook tests to
put the sibling tool-mempalace module on sys.path. Recorded so the next session
doesn't re-learn it.
