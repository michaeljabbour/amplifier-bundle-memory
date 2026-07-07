# Changelog

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
