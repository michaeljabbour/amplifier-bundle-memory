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

## 2026-09-27 — Three-layer memory, Amplifier-shaped (design session)

Evidence: `docs/research/2026-09-27-memory-systems-gene-survey.md`.
Design + task list: `docs/plans/2026-09-27-memory-layers-design.md`.
Entry format (for automated brainstorm/plan sessions): **Decision / Why /
Alternatives / Evidence / Status / Unblocks.** Status is one of
`ratified` (owner said so), `derived` (follows from a ratified rule, revisable),
`proposed` (needs owner ratification).

### D6 — Owner constraints for the layers work
**Decision:** (1) Amplifier-shaped and best-of-all-tools; (2) gene-transfer from
any surveyed repo; (3) vendor nothing; (4) amplifier-data (or other) is fine and
should be upgraded; (5) compaction follows standard Amplifier protocol; (6) build
the missing layers, small commits, decisions logged here.
**Why:** Michael, 2026-09-27, verbatim in design §1.
**Alternatives:** n/a — owner direction.
**Evidence:** conversation 2026-09-27.
**Status:** ratified. **Unblocks:** D7–D15.

### D7 — Memory ships no context manager; reflection observes standard events
**Decision:** A new hook `hooks-memory-reflect` subscribes to
`context:compaction` (emitted by foundation `context-simple` with stats only),
`context:pre_compact` (kernel constant; emitted by other managers) and
`session:end`. It reads the span since its watermark via the context module's
`get_messages()` (contract: always full, non-destructive history) rather than
depending on an evicted-messages payload that no contract guarantees.
**Why:** CONTEXT_CONTRACT makes compaction the context manager's private
policy; hooks are the sanctioned observers. A watermark works identically for
every conforming context manager.
**Alternatives:** keep/ship `context-sleep` (rejected: blocks the turn on an
LLM call, emits an unregistered event, competes with the user's chosen context
manager); require an `evicted` payload field (rejected: not in contract).
**Evidence:** core-expert (events.rs:105-110, CONTEXT_CONTRACT.md); installed
context-simple `__init__.py:1481-1501`.
**Status:** derived from D6(5). **Unblocks:** T2.5.

### D8 — Time and provenance are consumer-supplied facts, not substrate fields
**Decision:** `filed_at`, `in_session`, `at_commit`, `recorded_at`,
`expired_at`, `valid_at`/`invalid_at` are stored as amplifier-data facts (or
fact-cell payload fields), never as kernel event fields.
**Why:** amplifier-data deliberately excludes wall-clock time from events to
keep byte-identical regeneration (E1, `lenses/temporal.py:13-18`). Facts need
zero kernel change and are already queryable.
**Alternatives:** add timestamps to `CellWriteEvent` (rejected: breaks E1,
Rust model change).
**Evidence:** amplifier-data survey §2.
**Status:** derived. **Unblocks:** T0.2, T1.3, T2.1.

### D9 — Hybrid retrieval by rank fusion; BM25 lives upstream as a lens
**Decision:** add `lenses/bm25.py` to amplifier-data (pure fold, mirrors
`lenses/vector.py`); `store.search` fuses semantic + BM25 by RRF (k=60) with
per-arm caps on the existing shared `_SearchFold`, and reports per-arm ranks.
**Why:** Mem0, Hindsight and Graphiti independently converged here; our
current lexical term only reranks vector top-3k and cannot recover a vector
miss — fatal for identifiers, paths and error codes in a coding agent.
**Alternatives:** SQLite FTS5 sidecar (rejected for now: second store, second
consistency story); weighted sum (rejected: scale-sensitive, needs tuning).
**Evidence:** survey F5.
**Status:** derived from D6(4). **Unblocks:** T1.1–T1.2.

### D10 — Background LLM work = spawned agent via `session.spawn`, opt-in behaviors
**Decision:** distillation and index rollup run in spawned agent sessions
(`memory:distiller`, `memory:curator`) through the app-layer `session.spawn`
capability, launched in an `asyncio` task joined by `coordinator.register_cleanup`.
Model choice is `model_role: [fast, general]` in agent frontmatter. They ship as
opt-in `behaviors/memory-reflect.yaml` and `behaviors/memory-index.yaml`;
absence of `session.spawn` is a no-op (job stays queued), never an error.
**Why:** foundation: hooks are mechanism and must not call providers; the
spawn capability is what tool-recipes/dot-runner use; routing matrix owns
models.
**Alternatives:** in-hook provider call (rejected: policy leak, blocks turn);
work-tracker queue (deferred: heavier than a durable job cell for one consumer).
**Evidence:** foundation-expert; installed tool-recipes `executor.py`.
**Status:** derived. **Unblocks:** T2.4–T2.6, T3.1.

### D11 — Retire `context-sleep` from this bundle
**Decision:** stop treating `modules/context-sleep` as part of memory; remove
in P5 after confirming no consumer. Keep `docs/research/context-sleep-study.md`.
**Why:** it is a context manager (not memory), not composed by any behavior,
blocks on an LLM call, and is superseded by D7.
**Status:** derived from D6(5). **Unblocks:** T5.1.

### D12 — Facts are add-only, provenance-mandatory, supersede-never-delete
**Decision:** a fact cannot be written without ≥1 existing source drawer
(`@memory:derived_from`); updates create a new fact + `@memory:supersedes` +
invalidate `@memory:current` + `expired_at`; unresolved contradictions become
amplifier-data integrity tensions; single-valued predicates resolve newest-wins
without an LLM.
**Why:** Mem0 v2 dropped destructive UPDATE/DELETE; Graphiti/Mem0g invalidate
softly; our substrate is append-only; provenance keeps derived text auditable
against verbatim evidence (our differentiator).
**Status:** derived. **Unblocks:** T2.1, T2.7.

### D13 — The measurement bar for "best of all the tools"
**Decision:** design §6: AMB QA accuracy (LongMemEval-S, LoCoMo) ≥ v2.0.1 +5
and ≥ AMB hybrid baseline; PrecisionMemBench R@10 ≥ v2.0.1; ≤50% context
tokens; hot-path p95 ≤300 ms with zero LLM calls.
**Why:** our 96.6% R@5 is retrieval-only and partly synthetic; peers report
judged QA; vendor numbers are disputed, so we measure locally and publish both.
**Status:** **proposed — needs Michael's ratification of the numbers.**
**Unblocks:** T0.4 baseline, every later "better" claim.

### D14 — Secrets are scrubbed before capture
**Decision:** `hooks-memory-capture` redacts known secret shapes before filing;
emits counts only.
**Why:** verbatim capture + local-forever storage + search/briefing
resurfacing = a durable leak; the article's "keys live in the runtime, not in
memory notes" rule.
**Status:** derived from D6(1). **Unblocks:** T0.3.

### D15 — The Curator write path was never actually wired
**Decision:** treat `memory:curator_handoff_requested` as a defect: either
spawn the curator (D10 path) or delete the event (T2.6).
**Why:** `hooks-project-context` emits it at `session:end`; no subscriber
exists anywhere, so session-end curation described in docs never runs.
**Evidence:** `hooks-project-context/__init__.py:380-405`; grep of bundle.
**Status:** derived. **Unblocks:** T2.6.

### D16 — P0 as built (T0.1–T0.4): deviations and choices
**Decision / facts recorded:**
- T0.1: pin moved to `3ef751b6fac7f6899824b78774b74809f09bcd81` in **all six**
  module pyprojects (lockstep rule — uv `--no-sources` rejects transitive URL
  deps). Re-lock removed stale transitive packages (fastembed/onnx) from the
  briefing/project-context locks; those modules only declare amplifier-data, so
  this corrects pre-existing drift rather than changing behavior.
- T0.2: `filed_at` is **earliest-wins** per ref (content addressing means a
  re-filed identical drawer shares a ref; first-seen is the meaningful time).
  Folded in the existing `_SearchFold` pass — no per-hit regenerate.
  Legacy drawers without `filed_at` are **included** by garden's lookback and
  reported as `undated` (conservative: never silently drop pre-v2.1 memory).
- T0.3: redaction runs before the worthiness gate, previews and every event, so
  a secret cannot leak through `capture_skipped`/`capture_queued` previews
  either. `at_commit` is resolved without a subprocess (reads `.git`, worktree
  `gitdir:` files, loose refs, packed-refs) on the drain thread, cached per
  HEAD mtime. The capture hook tolerates an older store `file()` signature
  (TypeError retry) so module activation order cannot break capture.
- T0.4: AMB's CLI flag is `--split` (not `--domain`); splits are
  `longmemeval:s`, `locomo:locomo10`, `precisionmembench:single-turn`;
  `GEMINI_API_KEY`/`GOOGLE_API_KEY` is required by the AMB CLI regardless of
  judge. The adapter subclasses AMB types at runtime and injects into its
  `REGISTRY`; no AMB code is in this repo (AMB has no license).
**Status:** derived. **Unblocks:** baseline run (needs keys) → D13 ratification.

### D17 — P1 as built (T1.1–T1.4): hybrid retrieval
- **Upstream:** `amplifier-data` branch `feat/bm25-lens` adds `lenses/bm25.py`
  (`tokenize`, incremental `BM25Index`, `BM25Lens`); pure Python, no kernel
  change; tokenizer emits whole identifier runs AND their snake/camel sub-tokens
  so `ERR_4417` matches exactly (high IDF) and partially. Not re-exported from
  `lenses/__init__.py` (no concrete lens is). 10k docs build 55 ms, query 0.55 ms.
- **Consumer:** `store.search` defaults to `fusion="rrf"` (k=60, arms semantic +
  BM25, top N=max(3k,50) each); `fusion="legacy"` reproduces v2.0.1 byte-for-byte
  and the v2.0.1 equivalence tests are pinned to it. `score` keeps its legacy
  meaning so the interject cosine gate (0.72) and briefing rerank are unchanged;
  new fields `rrf`, `arms`. Persistent incremental BM25 index on the store
  instance (correct because the daemon is the single writer of an append-only log).
- **Capability check:** without `amplifier_data.lenses.bm25` the store silently
  runs legacy — so the current pin (3ef751b, no bm25) is safe to ship.
- **Temporal:** `since`/`until` filter on earliest `filed_at`; undated drawers are
  excluded when a bound is given (cannot prove they are in range).
- **Measured:** 5k-drawer p95 search: rrf 160.8 ms, legacy 157.0 ms (budget 300).
- **Pending owner action:** push `feat/bm25-lens` → merge → bump pin (T1.5).
**Status:** derived.

### D18 — P2 as built: facts, durable reflection jobs, compaction-observing hook
- **The conversation span is itself evidence.** `reflection_job_add` files the
  (redacted, newest-kept, ≤24k-char) span as an L1 drawer (category
  `conversation`), so every distilled fact can satisfy D12's
  provenance-mandatory rule by citing it. Evidence stays verbatim; facts stay
  auditable.
- **Jobs are cells**, state is a fact (`@memory:job_state` pending → done), so
  the queue is durable, append-only and survives crashes; no work-tracker
  dependency (D10 alternative kept deferred).
- **When the distiller runs:** spawned in the background on `context:compaction`
  / `context:pre_compact`; at `session:end` jobs are only queued (never delay
  exit); `session:start` drains up to 3 pending jobs in the background. Cleanup
  awaits running spawns ≤20 s, then cancels — cancelled jobs stay pending.
- **Spawn:** `session.spawn(agent_name, instruction, parent_session,
  agent_configs)` exactly as dot-runner's loop-agent calls it
  (`session_runner.py:504-541`). Child sessions are detected via
  `coordinator.parent_id` and never reflect (no recursion).
- **Redaction** moved into `tool-memory/redact.py` (capture keeps a shim) so the
  store redacts spans server-side and rejects secret-bearing facts.
- **Search** returns current facts as first-class hits (`layer: "fact"`,
  `derived_from`); `layers=` restricts. Legacy fusion unchanged.
- **Known limits (accepted):** two sessions draining at once may both process a
  job — the second `reflection_job_done` errors and identical facts dedup by
  content address. T2.6 (curator handoff event) moves to P3 with the index
  curator work; the event stays informational until then.
**Status:** derived. **Unblocks:** P3 (index rollup uses current facts).

### D19 — L3 index is mechanism-first; the curator improves it, never gates it
**Decision:** `index()` and `standing()` are deterministic, LLM-free reads,
computed on read from current facts and drawers in one fold. `index_set()` /
`standing_answer()` let `memory:curator` supersede the derived text with better
prose (with `@memory:cites` to facts); a curated index is served until
`pending_changes` > 10, then the derived view returns. Standing answers are
stale when any cited fact is no longer current (retraction check).
**Why:** D10's rule that background LLM work is additive, never load-bearing;
L3 must work in a store that never had a curator spawn.
**Alternatives:** require a curator write before reads (rejected: hard LLM
dependency); cache derived views (rejected: recompute is one fold, no
invalidation story needed).
**Status:** derived. **Unblocks:** layered briefing.

### D20 — P3/P5 as built
- **Layered briefing** (default when the client exposes `index`): coordination
  files first (byte-identical to legacy), then Memory map → Standing answers →
  Known facts `[proof N]` → Relevant evidence, sharing the existing budget.
  Legacy stays byte-identical and is pinned in its tests.
- **Include-order footgun fixed:** foundation deep-merges hook config by module
  id with the later scalar winning (`dicts/merge.py:79-129`), and a bundle's own
  declarations compose after its includes (`registry.py:792-795`). So
  `behaviors/memory-index.yaml` now `includes: memory:behaviors/memory-reflect`
  itself; its `index_refresh: true` wins regardless of consumer include order.
- **Index refresh** (opt-in): at `session:start` the reflect hook spawns
  `memory:curator` only when a derived room has ≥10 pending changes or a
  standing question is stale — quiet sessions spawn nothing.
- **Standing questions** are content-addressed by question text (a shared
  question across wings is one cell scoped to each) — accepted.
- **P5:** `modules/context-sleep` and `tests/test_context_sleep.py` removed
  after a consumer check across this repo and the four constellation siblings
  (zero references); research doc kept; CI list updated.
**Status:** derived.
