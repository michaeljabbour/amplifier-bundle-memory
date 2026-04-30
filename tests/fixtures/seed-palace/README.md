# Seed Palace Fixtures

This directory contains fixture files used to populate a MemPalace instance inside the
Digital Twin Universe (DTU) profile for end-to-end testing of the memory-bundle.

---

## What the files do

### `content/` — mined into the palace

All Markdown files under `content/` are ingested into the MemPalace palace via:

```bash
mempalace mine /workspace/seed-palace/content --mode files
```

The `--mode files` flag reads each `.md` file as a discrete memory fragment and lets
the capture hook classify each fragment by category (decisions, learnings, patterns, etc.)
based on keyword detection in the body text.

### `project-context/` — copied for the briefing hook

Files under `project-context/` are copied verbatim to `/workspace/project-context/` inside
the DTU container so that the briefing hook's `_find_project_context_dir()` function can
discover them.  That function walks upward from the current working directory (and also
checks `$PROJECT_CONTEXT_DIR` if set) looking for a directory named `project-context/`.

---

## Seeding flow

The DTU `memory-bundle-e2e.yaml` profile runs this sequence on startup:

1. **`mempalace init`** — creates a fresh `~/.mempalace/` store.
2. **`mempalace mine /workspace/seed-palace/content --mode files`** — populates the palace
   with the session-notes and architecture-decisions fragments.
3. **`cp -r /workspace/seed-palace/project-context /workspace/`** — places the briefing
   files where `_find_project_context_dir()` expects them.
4. **`cp -r ~/.mempalace ~/.mempalace-seed`** — freezes a clean snapshot so tests can
   call `reset-palace` to restore a known-good state between runs.
5. **`mkdir -p /workspace/spool`** — creates the spool directory that the capture hook
   writes event fragments to before the drain thread flushes them.

---

## The `reset-palace` script

A 3-line helper script is placed on `$PATH` in the DTU profile so tests can restore the
seeded state without re-running the full mine step:

```bash
#!/usr/bin/env bash
set -e
rm -rf ~/.mempalace
cp -r ~/.mempalace-seed ~/.mempalace
```

---

## How to extend

- **Add new memory fragments** — drop a `.md` file into `content/`.  It will be picked up
  by the next `mine` run.  Include trigger keywords (`decided`, `learned`, `pattern`, etc.)
  if you want the capture hook to classify the fragment into a specific category.
- **Add project-context documents** — drop a `.md` file into `project-context/`.  The
  briefing hook will include it in the next session briefing.

---

## Files

| Path | Purpose |
|------|---------|
| `content/session-notes.md` | Synthetic session notes mined as memory fragments |
| `content/architecture-decisions.md` | ADR log mined as memory fragments |
| `project-context/HANDOFF.md` | Current-work snapshot for the briefing hook |
| `project-context/PROJECT_CONTEXT.md` | Project overview for the briefing hook |
| `project-context/GLOSSARY.md` | Domain term definitions for the briefing hook |
