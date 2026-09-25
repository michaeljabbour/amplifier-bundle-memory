# Changelog

## [2.0.4] — 2026-09-25

### Changed (behavior -- read before upgrading)

- **Search is faster under continuous writes, but not free.** `_SearchFold`
  now extends a prior fold's payload join / per-subject index / embedding
  index with only the newly appended tail instead of rebuilding them from
  the whole log, and vector scoring runs through a numpy-vectorized cosine
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

The prior vendor-backed (ChromaDB) store is gone. Memory is now backed
entirely by the amplifier-data substrate through an auto-started local
memory daemon (a local ONNX embedder via fastembed; no torch, no external
network calls by default). This release lands the B3 phase of the cutover:
the mechanical rename sweep, the migration path, and the doc/DTU updates.
B1 (native daemon/client/embedder, additive) and B2 (tool + hooks rewired,
vendor code deleted) landed in this same release cycle.

#### Renamed (breaking)

| Old | New |
| --- | --- |
| `modules/tool-mempalace/` | `modules/tool-memory/` |
| `modules/hooks-mempalace-capture/` | `modules/hooks-memory-capture/` |
| `modules/hooks-mempalace-briefing/` | `modules/hooks-memory-briefing/` |
| `modules/hooks-mempalace-interject/` | `modules/hooks-memory-interject/` |
| `amplifier_module_tool_mempalace` (package) | `amplifier_module_tool_memory` |
| `amplifier_module_hooks_mempalace_*` (packages) | `amplifier_module_hooks_memory_*` |
| tool name `palace` | `memory` (operations unchanged: search/remember/status/kg/traverse/diary/mine/events/garden) |
| `PalaceTool` | `MemoryTool` |
| `MempalaceCaptureHook` / `MempalaceBriefingHook` / `MempalaceInterjectHook` | `MemoryCaptureHook` / `MemoryBriefingHook` / `MemoryInterjectHook` |
| `AmplifierDataMemoryStore` | `NativeMemoryStore` (the ONE store now) |
| `behaviors/mempalace.yaml` | `behaviors/memory.yaml` |
| `skills/mempalace/SKILL.md` | `skills/memory/SKILL.md` |
| config key `palace_path` | `home` (default `~/.amplifier/memory`) |
| `~/.mempalace/*` (store, events, spool) | `~/.amplifier/memory/*` (override: `AMPLIFIER_MEMORY_HOME`) |
| console script `mempalace-amplifier-data-gateway` | `memory-daemon` |
| console script `mempalace-load-captures` / `mempalace-write-cells` | `memory-load-captures` / `memory-write-cells` |
| console script `mempalace-server-concurrency-check` | `memory-daemon-concurrency-check` |
| console script `mempalace-dualwrite-compare` | retired (folded into `amplifier-memory-import --verify`) |
| event prefix `memory-mempalace:X` | `memory:X` |
| event schema | `v: 2` (hook names changed) |

#### Removed (breaking)

- The `mempalace` PyPI dependency, everywhere in this repo.
- The external SQLite fact-store module previously registered in
  `behaviors/memory.yaml` under the name `tool-memory`
  (`git+.../amplifier-module-tool-memory`) — it name-collided with the
  renamed native tool. Its niche (explicit key-value facts) is now covered
  by the native `kg` operation.
- `PalaceMemoryStore`, `DualWriteMemoryStore`, `_call_mcp_tool`, and the
  `shadow_gateway` config block (both `tool-memory` and
  `hooks-memory-capture`) — there is no shadow anymore; the daemon IS the
  store.
- The `[substrate]` optional-dependency extra on `tool-memory` — folded into
  hard dependencies (`amplifier-data`, `fastembed`).

#### Added

- **`amplifier-memory-import`** — one-shot, read-only migration from a
  legacy vendor store. Reads a ChromaDB `mempalace_drawers` collection
  (via the new `[migrate]` extra, chromadb only — never the vendor package
  itself), copies drawers and their embeddings verbatim (same MiniLM
  vector space, no re-embed by default; `--re-embed` opts into re-embedding
  through the daemon's current model), and writes through the memory
  daemon so the single-writer guarantee holds. Idempotent on re-run
  (content addressing + a read-before-write guard on facts and the
  embedding copy). `--verify` re-reads every imported drawer and
  byte-compares it against the source. The source directory is never
  modified. KG + diary import is honestly reported as skipped (no
  independently verifiable on-disk format for either without an installed
  copy of the vendor package — see `migrate.py`'s docstring).
- **Home-directory unification.** The event emitter, capture-hook spool,
  and daemon now all resolve the same `~/.amplifier/memory` home (override
  `AMPLIFIER_MEMORY_HOME`) and create it lazily on first use — there is no
  more "silent no-op unless already initialised" behavior.
- **DTU profiles**: `memory-native-e2e.yaml` (friend-scenario remember ->
  search round-trip through the auto-started daemon with the legacy vendor
  package asserted ABSENT; daemon crash-respawn) replaces
  `memory-bundle-e2e.yaml`; `memory-migration-e2e.yaml` seeds a real
  legacy-shaped ChromaDB store, uninstalls the vendor package, and asserts
  the migration report.
- **`tests/test_vendor_sweep.py`** — executable KG-N4 grep gate (zero
  `mempalace` outside the migration module and a small explicit allowlist;
  zero bare `palace` in `modules/`, `behaviors/`, `skills/`, `context/`,
  `agents/`, `bundle.md`, `README.md`).

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

- **Event observability** — all memory hooks now emit structured events to a per-session JSONL log at `~/.mempalace/events/{session_id}.jsonl`. Events include a ~100-char content preview and structured metadata (hook, event type, `ok`, `data`). Live tailing: `tail -f ~/.mempalace/events/*.jsonl`.
  - Events: `drawer_filed`, `capture_skipped`, `briefing_assembled`, `briefing_skipped`, `memory_surfaced`, `interject_skipped`, `coordination_read`, `coordination_scaffolded`, `curator_delegated`, `garden_completed`.
  - New config key `emit_events: true` on each hook (default on). Set `false` to disable per-hook.
  - New commit: `b9bcf6a`

- **`palace events` tool operation** — query the per-session JSONL event log from within a session. Supports `hook_filter`, `event_filter`, `tail` mode, and `limit` (cap 200). Returns `event_count`, `returned`, `skipped_lines`, and the events array. New commit: `532efad`

- **Curator Phase 3 — palace intelligence** — the Curator agent now enriches the knowledge graph at session:end with importance scores (0.0–1.0), category tags, and duplicate/related edges for every drawer filed in the session. Near-identical duplicates (cosine ≥ 0.95) are preserved verbatim and linked via `duplicates` KG edge with importance overridden to 0.15. Algorithm extracted into `phase3.py` pure functions for determinism and testability. New commit: `8013098`

- **Briefing importance re-ranking** — `hooks-mempalace-briefing` now fetches 8 candidates (up from 5) and re-ranks by `final = semantic + weight * (importance − 0.5) * 0.08` before truncating to top 5. New config key `briefing_importance_weight: 1.0` (default). Set to `0.0` for exact v1.1.0 behavior.
  - **Zero-regression guarantee**: untagged palaces (no `has_importance` KG facts) produce identical results to v1.1.0 — all boosts are 0.0.
  - **Benchmark** (200 synthetic drawers × 30 queries, simulated semantic scores, Phase 3 importance backfill):
    - R@5 baseline (weight=0.0): **0.567**
    - R@5 reranked (weight=1.0): **0.589**, Δ = **+0.022** ✅ PASS
  - Kill switch: set `briefing_importance_weight: 0.0` in `behaviors/mempalace.yaml` to revert to pure semantic ranking immediately.
  - New commit: `542872e`

- **`palace garden` tool operation** — on-demand deep analysis of a palace wing. Enumerates drawers, builds pairwise similarity adjacency via `mempalace_check_duplicate`, finds connected-component clusters via BFS, emits KG edges (`is_a`, `has_label`, `has_size`, `part_of_cluster`, `spans_rooms`), backfills `has_importance` for untagged drawers, and writes a Curator diary entry. Bounded by `garden_max_drawers` (default 200, hard cap 500) and 120-second wall-clock budget. New config key `garden_max_drawers: 200` on `tool-mempalace`. New commit: `425083e`

### Changed

- **`behaviors/mempalace.yaml`**: exposed new config keys (`emit_events`, `briefing_importance_weight`, `garden_max_drawers`). Behavior version bumped `1.2.0` → `1.3.0`.
- **`bundle.md`**: version `1.1.0` → `1.2.0`.
- **`tool-mempalace` module**: version `1.1.0` → `1.2.0` (new `events` and `garden` operations).
- **Hook modules** (`hooks-mempalace-capture`, `hooks-mempalace-briefing`, `hooks-mempalace-interject`, `hooks-project-context`): version `1.0.0` → `1.1.0` (emit_events wiring).
- **`agents/curator.md`**: Phase 3 KG enrichment instructions added (steps 10–12). Idempotency guidance updated: `mempalace_kg_add` uses upsert semantics — skip the pre-check on normal runs.

### Technical Notes

- All test suites pass: 105 tests in `tool-mempalace`, 12 in `hooks-mempalace-briefing`, 16 at bundle level. 2 integration tests skipped (mempalace CLI required).
- No external dependencies added to `tool-mempalace`: clustering uses `mempalace_check_duplicate` MCP calls, no direct ChromaDB access.
- `event_emitter.py` is thread-safe (module-level `threading.Lock`, append-mode writes, flush per call).
- Commits: `b9bcf6a`, `532efad`, `8013098`, `542872e`, `425083e`

---

## [1.1.0] — 2026-04-17

### Added
- **project-context integration**: new `hooks-project-context` module reads Tier 1 coordination files (`HANDOFF.md`, `PROJECT_CONTEXT.md`, `GLOSSARY.md`) at session start and delegates HANDOFF/PROVENANCE/GLOSSARY/WAYSOFWORKING updates to the Curator at session end. Scaffolds `project-context/` and `AGENTS.md` automatically on first run.
- **`context/project-context-guide.md`**: new context file that teaches agents the coordination file system, tier structure, and session protocol.
- **`hooks-mempalace-briefing`** now reads project-context Tier 1 files as a fourth briefing source. Works even when MemPalace is not installed (coordination files only mode).
- **`hooks-mempalace-capture`** now absorbs the category detection logic from `hooks-memory-capture` (decision, architecture, blocker, pattern, etc.) and enriches room names with the detected category. The separate `hooks-memory-capture` module is no longer needed.
- **Archivist agent** now reads `HANDOFF.md`, `PROJECT_CONTEXT.md`, `PROVENANCE.md`, and `EXPERIMENT_JOURNAL.md` on demand.
- **Curator agent** now has a Phase 2 (coordination file updates): updates HANDOFF.md, PROVENANCE.md, GLOSSARY.md, and WAYSOFWORKING.md at session end.

### Changed
- **`bundle.md`**: removed `generated_by` block; replaced with a clean `Credits:` line. Bumped version to 1.1.0.
- **`behaviors/mempalace.yaml`**: removed duplicate `context: include:` injection. Removed `tool-skills` re-declaration (inherited from foundation). Removed `hooks-memory-capture` (superseded). Added `hooks-project-context`.
- **Archivist agent**: trigger changed from `session:start` to `on_demand`. Session-start briefings are handled exclusively by `hooks-mempalace-briefing` to avoid double-briefing.
- **`context/instructions.md`**: updated to describe the two-layer architecture (palace + coordination files).

### Removed
- `hooks-memory-capture` from the behavior (superseded by `hooks-mempalace-capture` with built-in category detection).
- Duplicate `context: include:` in `behaviors/mempalace.yaml`.
- Duplicate `tool-skills` declaration in `behaviors/mempalace.yaml`.
- `generated_by: tool: manus` from `bundle.md`.

---

## [1.0.0] — 2026-04-17

### Added

- Initial release of `amplifier-bundle-memory`
- **Archivist agent** — read path: session briefings, semantic search, graph traversal
- **Curator agent** — write path: curation, deduplication, knowledge graph updates, diary
- **`tool-mempalace` module** — high-level `palace` tool with 7 operations (search, remember, status, kg, traverse, diary, mine)
- **`hooks-mempalace-capture` module** — auto-files verbatim tool outputs as palace drawers with auto-detected wing/room
- **`hooks-mempalace-briefing` module** — injects ephemeral wake-up briefing at session start (search + KG + diary)
- **MemPalace MCP integration** — all 29 MCP tools available via `mempalace_*` prefix
- **`mempalace` skill** — usage guide for agents on memory taxonomy, filing conventions, and tool patterns

### Consolidated (superseded modules)

- `amplifier-bundle-memory` → superseded by this bundle
- `amplifier-bundle-project-memory` → superseded by this bundle
- `amplifier-module-context-memory` → replaced by `hooks-mempalace-briefing`
- `amplifier-module-tool-memory` → replaced by `tool-mempalace`
- `amplifier-module-hooks-memory-capture` → extended by `hooks-mempalace-capture`
