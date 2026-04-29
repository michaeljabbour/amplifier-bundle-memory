# Project Context — memory-bundle

## What this project is

`amplifier-bundle-memory` is an Amplifier bundle that adds persistent, semantic memory to
an Amplifier agent session.  It provides hooks and tools that capture tool call results,
session summaries, and arbitrary fragments to a MemPalace palace, and retrieves relevant
context at session start via a briefing hook.

## Current phase

**Infrastructure hardening** — the core capture/recall/briefing pipeline is implemented
and passing unit tests.  The current focus is on DTU-based end-to-end tests that exercise
the full pipeline against real embedding APIs in an isolated container environment.

## Active milestone

Milestone: `v0.2.0 — DTU E2E`

- [x] Capture hook emitting `capture_queued` / `capture_skipped` synchronously
- [x] Drain thread deferring embedding + palace-write off the hot path
- [x] Briefing hook loading project-context documents from `_find_project_context_dir()`
- [x] Seed-palace fixture corpus for DTU profile
- [ ] `memory-bundle-e2e.yaml` DTU profile (in progress)
- [ ] `verify-seeding.sh` smoke test
- [ ] `reset-palace` helper script on `$PATH`

## Team

| Role | Notes |
|------|-------|
| Lead engineer | Owner of capture/drain architecture |
| Integration test | Owner of DTU profile and fixture corpus |

## Architecture pointers

Key files and directories:

| Path | Description |
|------|-------------|
| `behaviors/mempalace.yaml` | Amplifier behaviour definition — the entry point for `amplifier bundle add --app` |
| `modules/hooks-mempalace-briefing/` | Briefing hook — fires on `session:start`, injects palace recall into context |
| `modules/hooks-mempalace-capture/` | Capture hook — fires on `tool:post`, queues fragments for drain thread |
| `modules/tool-mempalace/` | Palace query tool — direct semantic search exposed as an Amplifier tool |
| `modules/tool-memory/` | High-level memory tool — wraps palace query with summarisation |
| `tests/` | Unit and contract tests |
| `tests/integration/` | Integration tests (require real API keys + DTU) |
| `tests/fixtures/seed-palace/` | Seed corpus and project-context documents for DTU profile |
| `.amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml` | DTU profile definition |

## Conventions

- **Synchronous `*_queued` / `*_skipped` events** — all `_queued` and `_skipped` event
  types are emitted synchronously on the hot path before any slow work begins.  Slow work
  (embedding calls, palace writes) is always deferred to a drain thread or subprocess.
- **`emit_events: false` kill switch** — setting `emit_events: false` in the hook config
  disables all event emission without disabling capture.  Used in tests that do not have
  an event bus available.
- **Integration tests use `subprocess.run()`** — integration tests launch Amplifier
  sessions via `subprocess.run()` rather than importing modules directly.  This ensures
  that the test exercises the full hook mount lifecycle and catches import-order issues.
