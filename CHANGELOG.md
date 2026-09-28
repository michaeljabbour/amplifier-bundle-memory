# Changelog

## [2.2.0] — 2026-09-28

Includes 2.0.2, 2.0.3, and 2.0.4 below (perf/incremental-fold, automation
opt-out, stale-daemon retirement, sub-session skip) alongside the new
three-layer memory work.

Three-layer memory (design: `docs/plans/2026-09-27-memory-layers-design.md`;
decisions: `project-context/PROVENANCE.md` D6–D20). L1 evidence (drawers)
is joined by L2 facts (derived, auditable, provenance-mandatory) and L3
index (room/wing navigation + standing questions), with hybrid retrieval,
opt-in cold-path reflection, and a mechanism-only retrieval-outcome event
pair that a separate conductor (behavioral-plasticity) consumes.

### Added

- **L2 facts** (D12): tool ops `fact_add`, `fact_supersede` (implicit via
  `supersedes`), `facts` (query, `current_only`). Add-only, provenance-
  mandatory (a fact cannot be written without ≥1 source drawer), supersede-
  never-delete. Single-valued predicates resolve newest-wins without an LLM;
  unresolved contradictions become amplifier-data integrity tensions (D12).
- **Reflection jobs, `hooks-memory-reflect`, `memory:distiller`,
  `context/reflection-rubric.md`** (D10, D18): opt-in
  `behaviors/memory-reflect.yaml` watermarks the conversation span since the
  last reflection on `context:compaction` / `context:pre_compact` /
  `session:end`, files it as a durable `reflection_job` cell, and spawns the
  distiller agent (`model_role: [fast, general]`) via the app-layer
  `session.spawn` capability — absence of that capability is a no-op (job
  stays queued), never an error.
- **L3 index + standing questions** (D19, T3.1/T3.3): tool ops `index`,
  `index_set`, `standing_add`, `standing_answer`, `standing`. Mechanism-first:
  `index()`/`standing()` are deterministic, LLM-free reads computed from
  current facts/drawers; the curator (opt-in `behaviors/memory-index.yaml`)
  supersedes the derived view with curated prose, served until
  `pending_changes > 10`. Standing answers go stale when a cited fact is no
  longer current.
- **Layered briefing** (D20, T3.2): coordination files → Memory map (L3) →
  Standing answers → Known facts `[proof N]` → Relevant evidence, within the
  existing token budget. Legacy briefing stays byte-identical and is pinned
  in its own tests; layered is the new default, with automatic fallback to
  legacy for daemons predating the L3 ops (capability probe, not a hard
  dependency).
- **RRF hybrid search + `since`/`until`** (D9, D17): a new `amplifier-data`
  BM25 lens fused with the existing semantic lens by rank fusion (k=60),
  reported per-hit as `rrf` + `arms` (rank per arm). Temporal `since`/`until`
  filters on earliest `filed_at`; undated drawers are excluded when a bound
  is given.
- **`filed_at` / `in_session` / `at_commit`** (D6, T0.2): drawer time and
  provenance facts, earliest-wins per content-addressed ref; `garden`'s
  `lookback_days` now filters by real age.
- **Secret redaction** (D14, T0.3): `hooks-memory-capture` and `fact_add`
  share one scrubber (`redact.py`) for provider API keys, GitHub/AWS/Slack/
  Google tokens, JWTs, PEM private-key blocks, bearer headers, and
  `NAME=secret`-shaped env assignments — applied before capture, previews,
  and event payloads alike.
- **Retrieval events, `memory:retrieved` / `memory:injected`** (T4.1): every
  memory retrieval (tool `search`/`facts`/`index`, `hooks-memory-interject`,
  `hooks-memory-briefing`) emits `memory:retrieved` (source, op, redacted
  query, wing, up to 20 content-free hit summaries, latency); content
  actually entering the model context additionally emits `memory:injected`
  (refs, per-ref layer, char count). Refs are content addresses — stable
  across sessions — closing the "stable memory ids + memory_outcome event"
  backlog item. Memory stays event-only by design (constellation rule,
  `AGENTS.md`): a separate conductor (behavioral-plasticity) is the only
  component that joins these events with its own outcome signal.
- **AMB adapter** (`benchmarks/amb/`, T0.4): registers this store as an AMB
  provider at runtime for LongMemEval-S / LoCoMo / PrecisionMemBench —
  dev tooling only, no AMB code vendored (AMB has no license).

### Changed

- `search` defaults to `fusion="rrf"` whenever the installed `amplifier-data`
  exposes `lenses.bm25` (capability check, not a hard dependency);
  `fusion="legacy"` reproduces v2.0.1 byte-for-byte and stays pinned in its
  own equivalence tests (D17).
- `hooks-memory-briefing` defaults to `briefing_mode="layered"`; legacy mode
  is retained (byte-identical output, own pinned tests) and is what an older
  daemon without the L3 index ops automatically falls back to (D20).
- `amplifier-data` pin moved to `3ef751b6fac7f6899824b78774b74809f09bcd81`
  across every module pyproject (lockstep rule — `uv --no-sources` rejects
  transitive URL deps across mismatched pins) (D16, T0.1), then to
  `2ecdce3e5c81e30fdab139d7f5d73a070a63af76` once `feat/bm25-lens` merged to
  `amplifier-data` `main` (D22) — still lockstep across all seven modules
  (`tool-memory`, `hooks-memory-capture/-briefing/-interject/-reflect`,
  `hooks-project-context`, `hooks-behavioral-write`).
- Search hits carry `importance` again end-to-end: this merge folds the
  scale-fix's scope/filing attribution and RRF fusion onto perf/incremental-
  fold's cached `_SearchFold` (D26) — the numpy-accelerated vector scorer
  now backs the RRF semantic arm too, and the scope index it needs comes
  from the fold's own precomputed membership rather than a fresh
  `fold_scope` walk per call.

### Removed

- `modules/context-sleep` is retired from this bundle (see
  `project-context/PROVENANCE.md` D11): it is a context manager, not memory,
  was not composed by any behavior, blocked the turn on an LLM call, and
  emitted an unregistered event. Superseded by `hooks-memory-reflect`, which
  observes standard compaction events. The empirical research behind it is
  kept at `docs/research/context-sleep-study.md`.

## [2.0.4] — 2026-09-25

### Changed (behavior -- read before upgrading)

- **Search is faster under continuous writes, but not free.** `_SearchFold`
  now extends a prior fold's payload join / per-subject index / embedding
  index with only the newly appended tail instead of rebuilding them from
  the whole log, and vector scoring runs through a numpy-accelerated cosine
  search (falls back to the exact pure-Python `VectorLens.query()` path
  when numpy is unavailable or embeddings have inconsistent dimensions).
  Measured on the real ~230k-event / 190MB store this closes an issue
  against: a search that used to cost ~2.4-3.2s per call under continuous
  concurrent writes now costs ~1.0-1.3s, essentially FLAT regardless of how
  many writes land between searches (1, 5, or 20 -- all ~1.0-1.3s), proving
  the incremental extension is doing its job. **The ~1s floor is NOT closed
  by this change**: `amplifier-data`'s `RustFileKernel.all_events()` (the
  only way to read the log at all -- confirmed via introspection, no
  incremental/windowed read API exists) re-marshals the ENTIRE event
  history into fresh Python objects on every call, and that alone measured
  0.35-0.85s depending on log size and disk-cache state. Closing that
  floor requires a change to `amplifier-data` (a separately pinned
  dependency), not this repo. When NO writes land between searches (the
  common case once Part B below reduces write volume), warm searches on
  the SAME fold hit ~0.1-0.2s.
- **`numpy>=1.24` is now a direct dependency** of `amplifier-module-tool-memory`
  (was already transitive via `fastembed`'s `onnxruntime`). Guarded import
  throughout -- never a hard requirement to search at all, only to search
  fast.
- **Automated/non-interactive sessions can opt out of memory entirely.** Set
  `AMPLIFIER_MEMORY_CAPTURE=off` (or `0`/`false`/`no`) in a process's
  environment, or add glob patterns to a hook's new `excluded_working_dirs`
  config list, to skip `hooks-memory-capture` (no write, no daemon
  contact), `hooks-memory-interject` (no search), and `hooks-memory-briefing`
  (no prefetch) for that process -- see `amplifier_module_tool_memory.
  automation_gate`. Default behavior for a normal interactive session
  (neither signal set) is unchanged.

### Fixed

- **`hooks-memory-briefing`'s early `mount()`-time prefetch ignored the new
  automation opt-out.** `on_session_start` skips correctly, but `mount()`
  itself unconditionally starts a background prefetch the moment the hook
  is mounted -- before `session:start` even fires. Both call sites now
  honor `automation_opt_out`.

## [2.0.3] — 2026-09-25

### Fixed

- **A stale daemon reporting the SAME version as the client no longer runs
  forever.** `client._discover()` only ever compared version *strings*
  (`_should_retire`); a reinstalled/editable `amplifier-module-tool-memory`
  package with no version bump left a daemon loaded with pre-optimization
  code running indefinitely even after the code that would have fixed it
  landed on disk. Measured 2026-09-25: a daemon started *before* the
  `_SearchFold`/`_CellRefCache` search optimizations were installed kept
  serving 2.8-3.4 s searches for a full day because both processes reported
  `2.0.2`. Every daemon now stamps a `code_fingerprint` (max `.py` mtime
  across its own installed package) into `/health` and `daemon.json`;
  `client._should_retire_stale_code()` retires and respawns a same-version
  daemon whose fingerprint is older than the current client's, falling back
  to comparing `daemon.json`'s `started_at` against the client's own
  fingerprint for daemons that predate this field. Retirement still uses the
  existing graceful `shutdown()`/`SIGTERM` path -- never `kill -9`.
- **`hooks-memory-interject` no longer searches memory for sub-agent
  (delegated) sessions.** Its `prompt:submit`/`tool:pre`/
  `orchestrator:complete` handlers fire for every session, including every
  `delegate()`-spawned child; a heavy user racked up 2,156 child sessions in
  30 days, each paying a full `ensure_daemon()` + `MemoryClient.search()`
  round trip. The coordinator stamps `parent_id` onto every emitted event
  (`amplifier_core.session.AmplifierSession.coordinator.hooks.
  set_default_fields`), so all three handlers now skip immediately -- no
  daemon contact at all -- whenever `data.get("parent_id")` is not `None`,
  mirroring `hooks-memory-briefing`'s existing `parent_id`-based
  sub-session skip for `session:start`.
- **The memory daemon warms its search fold in the background at startup.**
  Building the first `_SearchFold` snapshot over a large durable log takes
  several seconds (materializing the whole event log); `make_daemon()` now
  starts that build in a daemon thread immediately, so the daemon becomes
  healthy without waiting for it, and a real search racing the warm-up
  thread is still correct (`NativeMemoryStore._fold_snapshot`'s own
  single-flight generation/lock bookkeeping already covers concurrent
  builders).

## [2.0.2] — 2026-09-24

### Changed (behavior -- read before upgrading)

- **The wake-up briefing now reaches the model, which costs tokens.** Before
  2.0.2 the briefing was built at `session:start`, whose result amplifier-core
  discards, so nothing was ever delivered. `hooks-memory-briefing` 2.1.0
  prefetches it in the background from `mount()` and injects it at the first
  `prompt:submit` (waiting at most `deliver_wait_s`, 2 s; if still running it
  lands non-blocking at the next `provider:request`/`prompt:submit`). That
  adds roughly 300-1,500 tokens (`token_budget: 1500`) on turn one.
- **The briefing is NOT turn-one-only under the default orchestrator.** It is
  injected with `ephemeral=True`, but `loop-streaming`'s default
  `ephemeral_injection_mode: persist` stores it as a user message that
  `context-simple` protects from compaction, so it rides along (cached, but
  counted against the context window) on every later request of the session.
  The briefing footer, which used to claim it "will not appear in
  conversation history", no longer makes any claim about history.
- **Sub-agent sessions get no briefing by default.** Sessions whose
  `session:start` carries a `parent_id` are skipped unless
  `brief_subsessions: true`. (Before 2.0.2 they got nothing either -- nothing
  was delivered at all -- but the intended behavior changed.)
- **Memory sections can be up to 5 minutes old.** The prefetched search / KG /
  diary sections are reused per process for `cache_ttl_s` (300 s), so a second
  session in the same process within that window gets the first session's
  memory sections and misses writes made in between. Coordination files
  (HANDOFF.md etc.) are still re-read at delivery. Results where any lookup
  failed are never reused.
- **Upgrading retires a running 2.0.1 daemon automatically.** The first 2.0.2
  client to contact an older (numerically lower) daemon shuts it down and
  spawns a 2.0.2 one. Dev/unversioned clients (`0.0.0-dev`) and newer clients
  never retire a daemon (see Fixed).

### Fixed

- **The session-start briefing no longer blocks the first turn.** Its daemon
  round-trips (search, KG, diary, plus two per search hit for importance)
  held up the first prompt: 7.8-9.2 s with a warm daemon on a 178 MB store,
  30 s+ with a cold one. They now run off the critical path, concurrently, on
  daemon threads (an unfinished prefetch no longer holds process exit).
- **Concurrent daemon requests no longer fail with HTTP 400.** The durable
  Rust kernel raises `Already borrowed` when an append overlaps a read on
  another thread; the daemon now serializes kernel calls on the kernel's own
  lock (and logs a warning if a future amplifier-data makes that impossible).
  Live event logs showed ~1.4k briefing lookups lost this way.
- **Faster daemon reads, same results.** Search hits now carry `importance`
  (so the briefing drops 16 extra round-trips); per-event cell refs are
  memoized incrementally; per-hit lens reads and the vector index run over
  exact log slices; `read_diary`, `query_kg` and standalone `list_drawers`
  use the one-fold snapshot; and read paths stop appending duplicate
  scope/anchor cells to the log; a burst of reads shares one log snapshot
  (reused <= 5 s, only while nothing was appended; at most one snapshot is
  retained, so daemon memory stays at the 2.0.1 level). On a 178 MB /
  403k-event store: warm search 3.0 s -> ~1.0 s (the first search after
  daemon start is ~1.8 s), `read_diary` 1.3 s -> 0.4 s, `list_drawers` 38 s
  -> 0.4 s, degraded (embedder-cold) search from minutes to ~0.4 s.
- **A dev checkout or older client no longer shuts down the live daemon.**
  `ensure_daemon()`'s version-mismatch path retired ANY healthy daemon whose
  version differed from the client's -- including clients with no package
  metadata (`0.0.0-dev`: source checkouts, test venvs) -- and then usually
  failed to spawn a replacement from its own environment, leaving memory
  down for every session. It now retires only a strictly older daemon.
- **`hooks-memory-interject` honors `max_inject_chars`.** Once the first
  snippet was truncated to the cap, a negative slice bound kept almost the
  whole next memory (a 7,067-char injection was measured under the 800-char
  default). Separators now count too, so the cap is exact.

## [2.0.1] — 2026-08-24

### Fixed

- Memory search folds the append-only store once per query instead of once per
  result, removing the repeated-log-fold hot path on large stores.
- The interject hook no longer registers `tool:pre` retrieval by default and
  bounds prompt/orchestrator searches to three seconds, so memory fails open
  instead of adding a search round trip to every tool call.
- The daemon holds an OS-backed lifetime lock per memory home and removes
  `daemon.json` only when it still owns that discovery record, preventing
  duplicate daemons and older shutdowns from erasing newer discovery state.

## [2.0.0] — 2026-07-07

### BREAKING — Native cutover (docs/plans/2026-07-07-native-cutover-design.md)

The prior vendor-backed store (a third-party vector database) is gone. Memory
is now backed entirely by the amplifier-data substrate through an
auto-started local memory daemon (a local ONNX embedder via fastembed; no
torch, no external network calls by default). This release lands the B3
phase of the cutover: the mechanical rename sweep, the migration path, and
the doc/DTU updates. B1 (native daemon/client/embedder, additive) and B2
(tool + hooks rewired, vendor code deleted) landed in this same release
cycle.

#### Renamed (breaking)

The bundle's own module/package/tool names, config keys, home directory,
console scripts, and event prefix were all renamed away from the legacy
vendor's branding to neutral `memory`-prefixed names (e.g. `modules/tool-
memory/`, `MemoryTool`, `behaviors/memory.yaml`, `~/.amplifier/memory/*`,
`memory-daemon`, event prefix `memory:X`). The prior names carried the
legacy vendor's product branding directly (module names, an internal store
class name, a config key, and MCP-tool naming were all derived from it); the
full old-name -> new-name table is preserved in the external research
record (external research record: memthoughts/research/amplifier-memory-v2.1/,
not part of this bundle) rather than reproduced here, since reproducing it
requires spelling the retired branding out in full.

#### Removed (breaking)

- The legacy vendor's PyPI dependency, everywhere in this repo.
- The external SQLite fact-store module previously registered in
  `behaviors/memory.yaml` under the name `tool-memory`
  (`git+.../amplifier-module-tool-memory`) — it name-collided with the
  renamed native tool. Its niche (explicit key-value facts) is now covered
  by the native `kg` operation.
- The legacy vendor-backed store class, its dual-write fan-out class,
  `_call_mcp_tool`, and the `shadow_gateway` config block (both `tool-memory`
  and `hooks-memory-capture`) — there is no shadow anymore; the daemon IS the
  store.
- The `[substrate]` optional-dependency extra on `tool-memory` — folded into
  hard dependencies (`amplifier-data`, `fastembed`).

#### Added

- **`amplifier-memory-import`** — one-shot, read-only migration from a
  legacy vendor store. Reads the legacy vector backend's own drawer
  collection (via the new `[migrate]` extra, the legacy vector-backend
  client library only — never the vendor package itself), copies drawers
  and their embeddings verbatim (same MiniLM vector space, no re-embed by
  default; `--re-embed` opts into re-embedding through the daemon's current
  model), and writes through the memory daemon so the single-writer
  guarantee holds. Idempotent on re-run (content addressing + a
  read-before-write guard on facts and the embedding copy). `--verify`
  re-reads every imported drawer and byte-compares it against the source.
  The source directory is never modified. KG + diary import is honestly
  reported as skipped (no independently verifiable on-disk format for either
  without an installed copy of the vendor package — see `migrate.py`'s
  docstring).
- **Home-directory unification.** The event emitter, capture-hook spool,
  and daemon now all resolve the same `~/.amplifier/memory` home (override
  `AMPLIFIER_MEMORY_HOME`) and create it lazily on first use — there is no
  more "silent no-op unless already initialised" behavior.
- **DTU profiles**: `memory-native-e2e.yaml` (friend-scenario remember ->
  search round-trip through the auto-started daemon with the legacy vendor
  package asserted ABSENT; daemon crash-respawn) replaces
  `memory-bundle-e2e.yaml`; `memory-migration-e2e.yaml` seeds a real
  legacy-shaped vector-backend store, uninstalls the vendor package, and
  asserts the migration report.
- **`tests/test_vendor_sweep.py`** — executable KG-N4 grep gate (zero
  legacy vendor branding outside the migration module and a small explicit
  allowlist; zero bare legacy short-form branding in `modules/`,
  `behaviors/`, `skills/`, `context/`, `agents/`, `bundle.md`, `README.md`).

*(External research record for the full pre-scrub rename table and every
literal legacy identifier: memthoughts/research/amplifier-memory-v2.1/lineage.md,
not part of this bundle.)*

#### Migration instructions for existing users

```bash
pip install 'amplifier-module-tool-memory[migrate]'
amplifier-memory-import --verify
```

Then update your bundle pin to `@main` (or the tagged 2.0.0 release) and
re-add the bundle (`amplifier bundle add ...`) so `behaviors/memory.yaml`
is picked up. If you had the external SQLite `tool-memory` module composed
alongside this bundle, remove it from your own bundle config — the name
now belongs to the native tool.

### Technical Notes

- All module versions bumped to `2.0.0` (breaking rename + transport
  change) except `hooks-project-context`, which is functionally unchanged
  by the cutover and stays at `1.1.0`.
- Durability requires the amplifier-data Rust kernel; a Rust toolchain is
  now an install-time prerequisite (installing the pinned `amplifier-data`
  git dependency builds it via maturin).

---

## [1.2.0] — 2026-04-17

### Added

- **Event observability** — all memory hooks now emit structured events to a per-session JSONL log at the memory home's `events/{session_id}.jsonl`. Events include a ~100-char content preview and structured metadata (hook, event type, `ok`, `data`). Live tailing: `tail -f <memory-home>/events/*.jsonl`.
  - Events: `drawer_filed`, `capture_skipped`, `briefing_assembled`, `briefing_skipped`, `memory_surfaced`, `interject_skipped`, `coordination_read`, `coordination_scaffolded`, `curator_delegated`, `garden_completed`.
  - New config key `emit_events: true` on each hook (default on). Set `false` to disable per-hook.
  - New commit: `b9bcf6a`

- **`events` tool operation** — query the per-session JSONL event log from within a session. Supports `hook_filter`, `event_filter`, `tail` mode, and `limit` (cap 200). Returns `event_count`, `returned`, `skipped_lines`, and the events array. New commit: `532efad`

- **Curator Phase 3 — memory intelligence** — the Curator agent now enriches the knowledge graph at session:end with importance scores (0.0–1.0), category tags, and duplicate/related edges for every drawer filed in the session. Near-identical duplicates (cosine ≥ 0.95) are preserved verbatim and linked via `duplicates` KG edge with importance overridden to 0.15. Algorithm extracted into `phase3.py` pure functions for determinism and testability. New commit: `8013098`

- **Briefing importance re-ranking** — the briefing hook now fetches 8 candidates (up from 5) and re-ranks by `final = semantic + weight * (importance − 0.5) * 0.08` before truncating to top 5. New config key `briefing_importance_weight: 1.0` (default). Set to `0.0` for exact v1.1.0 behavior.
  - **Zero-regression guarantee**: untagged stores (no `has_importance` KG facts) produce identical results to v1.1.0 — all boosts are 0.0.
  - **Benchmark** (200 synthetic drawers × 30 queries, simulated semantic scores, Phase 3 importance backfill):
    - R@5 baseline (weight=0.0): **0.567**
    - R@5 reranked (weight=1.0): **0.589**, Δ = **+0.022** ✅ PASS
  - Kill switch: set `briefing_importance_weight: 0.0` in the legacy behavior file to revert to pure semantic ranking immediately.
  - New commit: `542872e`

- **`garden` tool operation** — on-demand deep analysis of a memory wing. Enumerates drawers, builds pairwise similarity adjacency via a duplicate-check op, finds connected-component clusters via BFS, emits KG edges (`is_a`, `has_label`, `has_size`, `part_of_cluster`, `spans_rooms`), backfills `has_importance` for untagged drawers, and writes a Curator diary entry. Bounded by `garden_max_drawers` (default 200, hard cap 500) and 120-second wall-clock budget. New config key `garden_max_drawers: 200` on the memory tool module. New commit: `425083e`

### Changed

- **legacy behavior file**: exposed new config keys (`emit_events`, `briefing_importance_weight`, `garden_max_drawers`). Behavior version bumped `1.2.0` → `1.3.0`.
- **`bundle.md`**: version `1.1.0` → `1.2.0`.
- **memory tool module**: version `1.1.0` → `1.2.0` (new `events` and `garden` operations).
- **Hook modules** (capture, briefing, interject, `hooks-project-context`): version `1.0.0` → `1.1.0` (emit_events wiring).
- **`agents/curator.md`**: Phase 3 KG enrichment instructions added (steps 10–12). Idempotency guidance updated: the KG-add op uses upsert semantics — skip the pre-check on normal runs.

### Technical Notes

- All test suites pass: 105 tests in the memory tool module, 12 in the briefing hook module, 16 at bundle level. 2 integration tests skipped (legacy vendor CLI required).
- No external dependencies added to the memory tool module: clustering uses a duplicate-check MCP call, no direct legacy vector-backend access.
- `event_emitter.py` is thread-safe (module-level `threading.Lock`, append-mode writes, flush per call).
- Commits: `b9bcf6a`, `532efad`, `8013098`, `542872e`, `425083e`

*(External research record for the full pre-scrub text of this release,
including every literal legacy module/tool/config name:
memthoughts/research/amplifier-memory-v2.1/lineage.md, not part of this
bundle.)*

---

## [1.1.0] — 2026-04-17

### Added
- **project-context integration**: new `hooks-project-context` module reads Tier 1 coordination files (`HANDOFF.md`, `PROJECT_CONTEXT.md`, `GLOSSARY.md`) at session start and delegates HANDOFF/PROVENANCE/GLOSSARY/WAYSOFWORKING updates to the Curator at session end. Scaffolds `project-context/` and `AGENTS.md` automatically on first run.
- **`context/project-context-guide.md`**: new context file that teaches agents the coordination file system, tier structure, and session protocol.
- **the briefing hook** now reads project-context Tier 1 files as a fourth briefing source. Works even when the legacy vendor store is not installed (coordination files only mode).
- **the capture hook** now absorbs the category detection logic from an earlier capture-hook generation (decision, architecture, blocker, pattern, etc.) and enriches room names with the detected category. The earlier capture-hook module is no longer needed.
- **Archivist agent** now reads `HANDOFF.md`, `PROJECT_CONTEXT.md`, `PROVENANCE.md`, and `EXPERIMENT_JOURNAL.md` on demand.
- **Curator agent** now has a Phase 2 (coordination file updates): updates HANDOFF.md, PROVENANCE.md, GLOSSARY.md, and WAYSOFWORKING.md at session end.

### Changed
- **`bundle.md`**: removed `generated_by` block; replaced with a clean `Credits:` line. Bumped version to 1.1.0.
- **legacy behavior file**: removed duplicate `context: include:` injection. Removed `tool-skills` re-declaration (inherited from foundation). Removed the earlier capture-hook module (superseded). Added `hooks-project-context`.
- **Archivist agent**: trigger changed from `session:start` to `on_demand`. Session-start briefings are handled exclusively by the briefing hook to avoid double-briefing.
- **`context/instructions.md`**: updated to describe the two-layer architecture (the legacy vendor store + coordination files).

### Removed
- the earlier capture-hook module from the behavior (superseded by the current capture hook with built-in category detection).
- Duplicate `context: include:` in the legacy behavior file.
- Duplicate `tool-skills` declaration in the legacy behavior file.
- `generated_by: tool: manus` from `bundle.md`.

*(External research record for the full pre-scrub text of this release,
including every literal legacy module name: memthoughts/research/
amplifier-memory-v2.1/lineage.md, not part of this bundle.)*

---

## [1.0.0] — 2026-04-17

### Added

- Initial release of `amplifier-bundle-memory`
- **Archivist agent** — read path: session briefings, semantic search, graph traversal
- **Curator agent** — write path: curation, deduplication, knowledge graph updates, diary
- **the memory tool module** — high-level tool with 7 operations (search, remember, status, kg, traverse, diary, mine), backed at this point by the legacy vendor store
- **the capture hook module** — auto-files verbatim tool outputs as memory drawers with auto-detected wing/room
- **the briefing hook module** — injects ephemeral wake-up briefing at session start (search + KG + diary)
- **legacy vendor MCP integration** — all 29 MCP tools available via a vendor-prefixed tool namespace
- **a usage-guide skill** — usage guide for agents on memory taxonomy, filing conventions, and tool patterns

### Consolidated (superseded modules)

- `amplifier-bundle-memory` → superseded by this bundle
- `amplifier-bundle-project-memory` → superseded by this bundle
- `amplifier-module-context-memory` → replaced by the briefing hook module
- `amplifier-module-tool-memory` → replaced by the memory tool module
- `amplifier-module-hooks-memory-capture` → extended by the capture hook module

*(External research record for the full pre-scrub text of this release,
including every literal legacy module/tool name: memthoughts/research/
amplifier-memory-v2.1/lineage.md, not part of this bundle.)*
