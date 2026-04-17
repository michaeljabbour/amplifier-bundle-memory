# Changelog

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
