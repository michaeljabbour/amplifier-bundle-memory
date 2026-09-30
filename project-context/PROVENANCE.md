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
compose attractor in the legacy behavior file (rejected: hard dependency).

### D3 — Emergent policy: declared, default off
**Decision:** `emergent.enabled` (default false) + `promote_threshold` live in the
manifest schema. The pipeline may propose emergent categories; the user confirms.
Not yet wired into classification logic.
**Why:** Keep the precision/recall knob visible and user-owned, but don't ship
auto-promotion behavior unproven. "Propose, don't drop, don't silently park."

### D4 — Substrate: the legacy vendor store now, amplifier-data as a declared seam
**Decision:** Phase 2 writes through a legacy-vendor-backed `MemoryStore`
implementation (the previous vendor's own vector store). An amplifier-data-
backed store exists as a loud `NotImplementedError` stub.
**Why:** amplifier-data's Rust floor is proven (E1–E3 green) but it lacks
persistence + a vector lens — the two things memory actually needs. Writing
through a `MemoryStore` seam means swapping to amplifier-data later is a
one-class change, with the gap explicit rather than silent.
**Open fork for Michael:** commit to amplifier-data (fund persistence + vector
lens) vs. stay on the legacy vendor store and keep amplifier-data
concept-only. Phase 3 gated on this.
**(Superseded by D5, 2026-07-07 — the stub described here was completed.)**
*(External research record for the full pre-scrub text of this decision:
memthoughts/research/amplifier-memory-v2.1/lineage.md, not part of this
bundle.)*

### D5 — Substrate seam completed (supersedes D4's "stub" status)
**Date:** 2026-07-07. Implements
`docs/plans/2026-07-07-substrate-adapter-completion-design.md`.

**Fork resolution:** D4's "open fork" (commit to amplifier-data vs. stay on
the legacy vendor store only) resolved toward **composition**: amplifier-data's persistence
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
  policy per COMPOSITION.md stays with the embedder, e.g. the legacy vector
  backend's own embedding model); it is dim-agnostic by construction.
- **KG facts** — anchor-cell encoding: a legacy-store string entity (e.g.
  `"svc-a"`) maps to a content-addressed `entity:{name}` cell so
  `assert_kg`/`invalidate_kg`/`query_kg`/`kg_timeline` can carry legacy-shaped
  string triples onto substrate `(Hash, str, Hash)` facts. Serves both the
  legacy-vendor-store tool's `kg` op (shadowed best-effort via `_shadow_kg`) and
  Phase-3 curator facts (`has_importance`/`has_category`/`duplicates`/
  `related_to`) — no special-casing, same `assert_kg` surface.
- **Diary** — `file_diary(agent_name, entry, topic)` introduces a NEW scope
  axis, `agent:{agent_name}`, orthogonal to `wing:`/`room:`. Shadowed
  best-effort from the legacy store's `diary write` op via `_shadow_diary`.

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
- **Read-path cutover** — the legacy vendor store remains the ONLY production
  read source. The new `dualwrite_compare.py` checks (vector self-retrieval, KG
  assert/invalidate/timeline, scope-query consistency, diary round-trip) are
  verification that the substrate COULD answer, not a migration. Cutover
  policy is a future design decision.

### Process note — test placement
Discovered that repo-root `tests/` is DTU-gated (skipped outside the
memory-bundle-e2e container). Unit tests for this work were placed under
`modules/*/tests/` instead, with a local conftest for the capture-hook tests to
put the sibling legacy-vendor-store tool module on sys.path. Recorded so the
next session doesn't re-learn it.

*(External research record for the full pre-scrub text of D5:
memthoughts/research/amplifier-memory-v2.1/lineage.md, not part of this
bundle.)*

## 2026-09-27 — Three-layer memory, Amplifier-shaped (design session)

Evidence: external research record: memthoughts/research/amplifier-memory-v2.1/
(not part of this bundle).
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
**Why:** multiple surveyed systems independently converged here; our
current lexical term only reranks vector top-3k and cannot recover a vector
miss — fatal for identifiers, paths and error codes in a coding agent.
**Alternatives:** SQLite FTS5 sidecar (rejected for now: second store, second
consistency story); weighted sum (rejected: scale-sensitive, needs tuning).
**Evidence:** survey F5 (external research record:
memthoughts/research/amplifier-memory-v2.1/, not part of this bundle).
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
**Why:** a surveyed system dropped destructive UPDATE/DELETE in a later
version; other surveyed systems invalidate softly; our substrate is
append-only; provenance keeps derived text auditable against verbatim
evidence (our differentiator).
**Status:** derived. **Unblocks:** T2.1, T2.7.

### D13 — The measurement bar for "best of all the tools"
**Decision:** design §6: AMB QA accuracy (LongMemEval-S, LoCoMo) ≥ v2.0.1 +5
and ≥ AMB hybrid baseline; PrecisionMemBench R@10 ≥ v2.0.1; ≤50% context
tokens; hot-path p95 ≤300 ms with zero LLM calls.
**Why:** our 96.6% R@5 is retrieval-only and partly synthetic; peers report
judged QA; vendor numbers are disputed, so we measure locally and publish both.
**Status:** ratified (Michael, 2026-09-28: "Okay, on D13").
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

### D21 — P4 as built + v2.1.0 release prep
- `memory:retrieved` (source tool|interject|briefing, op, redacted query ≤200
  chars, wing, ≤20 content-free hits {ref, rank, layer, score, rrf, arms},
  latency_ms) and `memory:injected` (source, refs, layers, chars) are emitted
  and registered via observability.events. Refs are content addresses, so they
  are stable ids across sessions — this closes the backlog item "stable memory
  ids + memory_outcome event": the conductor joins `memory:injected` with its
  own outcome signal. Memory stays event-only (constellation rule).
- Versions → 2.1.0 in lockstep (hooks-project-context jumped 1.1.0 → 2.1.0 for
  bundle-wide consistency); `daemon_version()` reads package metadata, so an
  old 2.0.1 daemon respawns and serves the new ops. hooks-memory-reflect 0.1.0.
  All module locks re-generated (`uv lock --check` clean in every module).
- 2.1.0 not 3.0.0: every default change keeps a byte-identical legacy mode
  (`fusion="legacy"`, `briefing_mode="legacy"`), and all new layers are
  additive or opt-in.
**Status:** derived.

### D22 — amplifier-data stays a separate repo; merged to main, not absorbed
**Decision:** merge `feat/bm25-lens` into amplifier-data `main` and pin to it;
do not fold amplifier-data into this bundle.
**Why:** the owner allowed either (2026-09-28). amplifier-data is the shared
substrate of the constellation (the conductor and survey depend on it); the
one-way edge memory → amplifier-data (AGENTS.md) exists so memory never owns
other bundles' storage. Absorbing it would invert that edge.
**Status:** derived from the owner's permission; revisable.

### D23 — No remnants of original products
**Decision:** no product names (the legacy vendor store and the ten surveyed
systems) anywhere in this repo or amplifier-data, enforced by
`tests/test_no_product_names.py` (and its amplifier-data twin). The only
exemption is the external benchmark harness's own identifiers inside
`benchmarks/amb/`. The legacy-import tool (`migrate.py`,
`amplifier-memory-import`, the vector-DB extra) and vendor-era design docs were
removed; attribution and verbatim lineage now live outside the product in
`memthoughts/research/amplifier-memory-v2.1/` (`gene-survey.md`, `lineage.md`).
**Why:** owner: "we don't want any remnants of the original products in our
work." Evidence stays auditable, just not inside the product.
**Open (owner):** the drawer/wing/room vocabulary is the legacy vendor's
metaphor and is public API (tool params `wing`/`room`, hit fields, events). A
rename is a breaking v3 change — not done without a ruling.
**Status:** ratified (directive) except the open item.

### D24 — Lessons from memthoughts (next-phase candidates)
Source: memthoughts gap map, wikimem loop, and the MemoryBench paper (see
memthoughts/research/). Surveyed systems did not consistently beat plain
BM25/embedding retrieval outside the long-input/short-output task shape; the
differentiator was procedural memory learned from feedback.
1. **Procedural (skill) memory keyed by task signature**, written from outcome
   feedback — our biggest remaining gap. (proposed, medium)
2. **Measure across task shapes**, not only long-context QA: add a second
   benchmark covering short/long input × short/long output with feedback logs
   before claiming "best of all tools" under D13. (proposed, low–medium; needs
   GPU or API budget)
3. **Construction cost is a first-class metric:** record distill time and
   tokens per compaction next to search p95. (derived, low)
4. **Growth bound on LLM-authored writes:** per-room fact cap / rollover rule
   (the fact gate already rejects missing sources and secrets). (proposed, low)
5. **Decay of uncorroborated derived facts** (never evidence): e.g. facts with
   proof_count 1 never retrieved in N days leave `@memory:current`
   (supersede-style, reversible). (proposed, medium)
Rejected: rewrite-in-place topic pages (conflicts with supersede-never-delete
and unproven at scale). A reconstruct-with-citations answer step stays a
candidate after the baseline shows whether it's needed.
**Status:** proposed.

### D25 — Agent files never declare relative module sources
**Decision:** removed `tools:` from `agents/distiller.md`; spawned agents
inherit the parent's tools, and memory-reflect requires memory.yaml (which
mounts tool-memory). `tests/test_agent_frontmatter.py` (in CI) fails on any
relative `source:` in `agents/*.md`.
**Why:** DTU install check (2026-09-28): foundation copies agent `tools:`
verbatim and resolves relative sources against the base path of the LAST
composed bundle (the CLI composes its default behavior last), so every session
failed strict activation when memory-reflect/memory-index was composed. Unit
tests could not see it — only a real composition could.
**Status:** derived (defect fix).

### D26 — First D13 baseline: RRF helps, the bar is not met, scale is the real gap
**Results (see EXPERIMENT_JOURNAL 2026-09-28):** PrecisionMemBench R@10
0.941 → 0.970 (met); LongMemEval-S QA 55.8% → 58.2% (+2.4, not significant;
bar +5 unmet); context tokens unchanged (bar unmet — L1 retrieval returns the
same volume; the token lever is the L2/L3 briefing, which the harness does not
exercise); p95 130 ms on a small store but ~9 s on a 316 MB store for both
fusions, and wing-scoped R@10 drops 0.975 → 0.875 as other wings accumulate.
LoCoMo and L2 facts unmeasured.
**Decision:** the next work item is store scale, ahead of any new layer:
(1) root-cause the scoped-recall loss as the store grows; (2) make per-query
cost sub-linear in total log size (persistent incremental indexes in the
single-writer daemon instead of a full-log fold per query).
**Also recorded:** the benchmark adapter had three defects that made it unable
to run (no base class; directory passed as store path; scores-only raw
response scored 0%) — fixed before any number was recorded.
**Status:** derived; D13 remains the bar.

### D27 — Integrated origin/main (v2.0.2–2.0.4) under the layers; release is 2.2.0
**Decision:** merged origin/main (18 commits since the branch's base) into
`feat/memory-layers`: main's reviewed mechanisms are the base (incremental fold
caching, numpy vector search, serialized kernel access, bounded snapshot
retention, briefing prefetch + once-per-session delivery at the first
`prompt:submit`, sub-session skip, strictly-older daemon retirement); the
layers were grafted on. Lockstep modules → 2.2.0 (main shipped briefing 2.1.0).
**Why:** the branch was cut from a stale local main. The DTU showed our
briefing listened only on `session:start`, whose HookResult the kernel discards
— main already delivered at `prompt:submit`. Taking main's chassis fixed that.
**Results:** ~851 tests pass; product-name gate clean; rrf p95 94–100 ms,
legacy 75–79 ms at 5k drawers (was 232–297 / 225–236 before merging main).
The scope-aware attribution dicts are now extended incrementally inside main's
fold cache (D26 item 2, partly). Also caught: our search had dropped main's
`importance` hit field — restored.
**Still open:** scoped p95 grows linearly with UNRELATED corpus size
(6 → 173 → 603 ms at 0/20/60 noise wings × 200 drawers) because the vector and
BM25 arms still fold the whole log; the next substrate step is scope-partitioned
indexes maintained from the kernel's `subscribe` stream.
**Status:** derived.

### D28 — DTU validation of fb51886: 9/9 pass; two follow-ups
**Evidence (DTU memory-layers-e2e, 2026-09-28):** strict activation (both
bundles); capture + redaction with filed_at/in_session/at_commit; hybrid search
with per-arm ranks and importance; briefing delivered at first prompt:submit
(HANDOFF marker quoted verbatim, Memory map + Known facts present);
compaction/session-end reflection → distiller wrote a `procedure` fact
(`predicate: test_command`) and a `preference` fact (`predicate:
package_manager`), both `derived_from` the span drawer; queue-at-end /
drain-at-start; content-free retrieval events; product-name gate; no daemon
errors. The earlier "capture FAIL" was a test input with no category keyword —
the category allowlist working as designed, not a defect.
**Follow-ups (open):**
1. **Stale daemon after an in-place bundle update:** `daemon_version()` reads
   installed package metadata; a bundle cache refresh without reinstalling the
   editable modules leaves the old dist-info, so a 2.1.0 daemon survived an
   update to 2.2.0 source. Fix direction: derive the daemon's identity from
   the module source (version constant or code fingerprint), not dist-info.
2. **Query text in `memory:retrieved`:** the live prompt/search string
   (redacted, ≤200 chars) is recorded for correlation. Consider a
   `query_mode: text|hash|off` option before this telemetry leaves the machine.
**Status:** derived.

### D29 — P6: meet D13 (levers, in order of expected impact)
**Diagnosis from D26:** each drawer is a whole conversation session, so k=10
hits hand the answerer ~29k tokens of mostly noise (tokens bar unmet, and the
noise costs accuracy); temporal questions regressed under RRF because evidence
reaches the answerer without dates; scoped latency grows with unrelated data.
**Plan:**
- T6.1 per-wing partitioned vector + BM25 indexes, maintained incrementally
  in the daemon's fold cache — scoped cost O(wing), not O(store).
- T6.2 passage index: long drawers are split into overlapping passages
  (derived cells, `@memory:passage_of` → drawer; drawer stays verbatim L1
  evidence); search ranks passages, returns the passage text + parent ref.
- T6.3 date-stamped evidence: every rendered hit carries its filed/observed
  date.
- T6.4 optional local cross-encoder rerank of the fused top-N (same local
  embedding runtime; no network), enabled only if it stays inside the p95 budget.
- T6.5 embedder choice as config (current 384-d model vs a stronger 384-d
  retrieval model) with a re-embed sweep.
- T6.7 daemon identity from module source, not installed metadata (D28.1).
- T6.6 measure each lever on a fixed stratified LongMemEval-S subset, then
  full LongMemEval-S, LoCoMo and PrecisionMemBench on the winning config.
**Status:** derived from D13 (ratified).

### D30 — P6 as built (T6.1–T6.5, T6.7)
- **T6.1 partitions:** per-wing incremental numpy matrices + per-wing BM25
  indexes inside the fold cache; room applies as an intersection within the
  wing. Scoped p95 with 0/20/60 unrelated wings: 0.46/0.44/0.45 ms (was
  6/173/603). T1.4 (5k drawers): rrf 85–103 ms. Fixed on the way: a room-only
  scope silently searched every wing that shared the room name.
- **T6.2 passages:** drawers ≥1200 chars get ~900-char line-boundary passages
  (150 overlap) as derived cells `@memory:passage_of` → drawer, embedded and
  indexed like drawers; backfill sweep for existing drawers (500/run). Search
  default granularity is passage under rrf (≤2 passages per drawer); drawer
  mode rolls up by best passage. Passage writes are a second idempotent commit
  after the drawer's batch (a crash in between self-heals via backfill).
- **T6.3 dates:** briefing, interject and the benchmark adapter render
  `[YYYY-MM-DD]` from observed_at → filed_at; facts render with proof count;
  legacy briefing unchanged.
- **T6.4 rerank:** local 80 MB MS-MARCO MiniLM-L6 cross-encoder via the same
  runtime; lazy singleton; unavailable → neutral, never raises. Defaults
  top-20 candidates × 800 chars (30 × 900 measured p95 160 ms, over budget).
  Off by default until the benchmark shows it pays.
- **T6.5 embedder:** `embedding_model` option (current MiniLM-L6 default,
  bge-small-en-v1.5 available); embeddings record `embedded_with`; vectors from
  another model leave the semantic arm and are re-queued — switching is safe.
- **T6.7 daemon identity:** version from a source constant (`_version.py`) +
  sha256 code fingerprint; equal version + different fingerprint → retire and
  respawn; newer daemons never retired; pyproject == `__version__` test.
**Verification:** 801 module/bench + 92 root tests, twice, green; name gate clean.
**Status:** derived.

### D31 — D13 campaign #2 (T6.6): 4 of 6 bar items met
**Results (EXPERIMENT_JOURNAL 2026-09-29):** winner config C = rrf + passage
granularity + dates (rerank off, MiniLM embedder).
- LoCoMo QA 67.7 → **81.4** (+13.7, significant) — met; beats the harness's
  own hybrid baseline run like-for-like (74.5) — met.
- Context tokens 8% (LongMemEval-S) / 20% (LoCoMo) of the old path — met.
- PrecisionMemBench R@10 0.970 ≥ 0.941 — met.
- LongMemEval-S QA 55.8 → 59.2 (+3.4, not significant) — **unmet**; subset vs
  hybrid 57 vs 61 (not significant) — unsettled.
- Full-store p95 — **unmet**: warm searches 6–7 ms, but the store drops its
  in-memory fold after 5 s idle and rebuilds from the whole log (384–460 s at
  1 GB); first search per wing up to ~3 s.
**Per-type:** dates lifted temporal questions (LME 36.8 → 56.4, LoCoMo 45 →
76); passages cut multi-session (57.9 → 39.1) — ten hits capped at two per
session cover fewer sessions.
**Rejected levers (subset evidence):** cross-encoder rerank (−3 QA, lower R@5,
extra latency); bge-small embedder (tie on QA, 37% slower ingest). Both stay
available as options, off by default.
**Found by the campaign:** the passage splitter only cut at line breaks, so
single-line sessions became one drawer-sized passage (fixed: over-long lines
split at whitespace; window-end cuts fixed); the adapter never embedded
passages (fixed). Subset noise is ±3–7 QA points: choices are made on the
subset, claims only on full runs.
**Status:** derived.

### D32 — P7: close the remaining bars
- T7.1 **Indexes never expire and never rebuild from the log on the hot path.**
  The daemon is the single writer, so it updates its derived indexes
  (per-wing vectors, per-wing BM25, scope/filing/fact maps) write-through at
  write time; the raw event snapshot is not retained; content is resolved by
  ref. One full fold at daemon start only.
- T7.2 **Batch passage writes** with their drawer (one append per drawer).
- T7.3 **Multi-session recall:** pack evidence to a token budget with a
  diversity-first pass (one passage per drawer before second passages), tuned
  only on the fixed subset.
- T7.4 **Legacy-fingerprint transition:** a same-version daemon reporting a
  non-string (pre-fix timestamp) fingerprint is older code by construction →
  retire it.
- Then: full LongMemEval-S for the tuned config and the hybrid baseline;
  full-store latency measured after idle.
**Status:** derived from D13.

### D33 — P7 as built: bar (f) met on the real 1 GB store
- **T7.1 write-through indexes:** once warm, search reads no log; one
  `os.stat` detects cross-process appends (background catch-up on a fresh
  kernel handle, never blocking); every write path funnels through one
  commit-and-apply choke point that updates the derived indexes from the
  events it just appended, under a dedicated index lock (search runs without
  the daemon write lock). Idle never discards the index. Hit content resolves
  through a bounded LRU; vectors are float32; zero-norm / non-finite vectors
  never enter a matrix (the NaN matmul warning is gone).
- **Eager build:** the daemon warm-up primes every wing's partition in the
  background after it starts serving.
- **Measured independently (1.05 GB store, 337 wings, 40 real queries):**
  warm p50/p95 5.9/48.9 ms, post-idle p95 6 ms (was 194 ms/3.3 s and 3.3 s;
  originally 384–460 s after idle); first search after build 5 ms. One-time
  eager build 613 s; peak RSS 6.3 GB (was ~8 GB).
- **T7.2:** drawer + passages commit in one batch (ingest ~2× faster).
- **T7.3:** benchmark adapter can pack to a token budget, one passage per
  session before any second passage; the briefing's evidence section uses the
  same diversity-first rule. Tuning pending (subset only).
- **T7.4:** a same-version daemon reporting a legacy (numeric) or missing
  fingerprint is retired; newer daemons never.
**Open:** restart cost at extreme scale (persisted index checkpoint) and RSS
(the in-RAM event list remains the largest contributor).
**Status:** derived; D13 (f) met.

### D34 — Campaign #3: packing didn't help; the gap is thin context, not coverage
**Results (EXPERIMENT_JOURNAL 2026-09-29, campaign #3):** budget packing
(6k/10k tokens) did not beat C on the subset; full LongMemEval-S C = 59.4%
(strict 59.0; reproduces #2 within 1 pt) vs A 55.8 (p=0.20) and the harness's
hybrid baseline 62.2 (p=0.30). Multi-session: C 51/133 vs hybrid 77 (p=0.0005);
temporal: C 78 vs hybrid 51 (p=0.0003). 60 of C's 79 multi-session misses had
every gold session in context — the answerer lacked the detail inside them.
**Decision (P8):** "rank on passages, read on neighborhoods": the store
returns, for the top hits, adjacent passages of the same drawer (or the whole
drawer when small), packed to a token cap ≤ 50% of A. Unconditional variants
are preferred; a question-shape-conditional variant is tried only as an
ablation and flagged as benchmark-tuned if chosen. Choices on the subset only;
one full LongMemEval-S confirmation; LoCoMo regression check.
**Status:** derived.
