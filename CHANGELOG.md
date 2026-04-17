# Changelog

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
