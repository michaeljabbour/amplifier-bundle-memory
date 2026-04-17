# Memory System

This bundle provides a **local-first, two-layer AI memory system**:

1. **MemPalace** — semantic palace storage with 96.6% R@5 retrieval on LongMemEval, knowledge graph, agent diaries, and cross-wing graph traversal. Nothing leaves your machine.
2. **project-context** — structured coordination files (`PROJECT_CONTEXT.md`, `HANDOFF.md`, `GLOSSARY.md`, etc.) that persist in the repo, survive clones, and are read by every AI coding tool.

## Session Lifecycle

**Session start** — `hooks-mempalace-briefing` fires first, assembling an ephemeral wake-up briefing from palace semantic search + knowledge graph + agent diary + Tier 1 coordination files. `hooks-project-context` reads `HANDOFF.md` and `PROJECT_CONTEXT.md` to surface what was being worked on.

**During work** — `hooks-mempalace-capture` runs silently on every tool output, auto-detecting the project wing and topic room, deduplicating, and filing verbatim drawers into the palace. No LLM cost for routine captures.

**Session end** — The **Curator** agent processes the session: deduplicates and categorizes palace captures, updates the knowledge graph, writes a diary entry, and updates `HANDOFF.md` with a structured handoff for the next session.

## Palace Structure

| Level | Concept | Example |
|---|---|---|
| **Wing** | A project or person | `wing_myapp`, `wing_alice` |
| **Room** | A topic within a wing | `auth-migration`, `api-design` |
| **Drawer** | Verbatim stored content | The actual text chunk |
| **Tunnel** | Cross-wing connection | `myapp/auth` ↔ `team/alice` |

## Tools

| Tool | Description |
|---|---|
| `palace_search` | Semantic search across the palace (scoped by wing/room) |
| `palace_remember` | File verbatim content into a specific wing and room |
| `palace_status` | Palace overview: drawer count, wings, rooms |
| `palace_kg` | Query or update the knowledge graph |
| `palace_traverse` | Walk the palace graph to find connected ideas |
| `palace_diary` | Read or write to the agent diary |
| `palace_mine` | Mine a directory or conversation file into the palace |
| `memory_set` / `memory_get` | Explicit SQLite key-value facts (fast, no semantic search) |

All 29 MemPalace MCP tools are also available directly via the `mempalace_*` prefix.

## Key Behaviors

**Verbatim storage** — Content is stored as-is, never summarized. This is what makes retrieval accurate at 96.6% R@5.

**Scoped search** — Always pass `wing` and/or `room` filters when context is clear. Unscoped searches are slower and less precise.

**Knowledge graph** — Entity relationships with temporal validity windows. Use `palace_kg` to track who works on what, what decisions were made when, and what has changed.

**Agent diaries** — Each agent gets its own wing and diary. Use `palace_diary` to record reasoning, not just outcomes.

**Coordination files** — `project-context/HANDOFF.md` is the human-readable session handoff. The Curator updates it at session end. Read it at session start via the briefing hook.

## Setup

```bash
# Install MemPalace
pip install mempalace

# Initialize a palace for your project
mempalace init ~/projects/myapp

# Verify the MCP server works
mempalace mcp --help
```

Once initialized, this bundle connects automatically via the MCP stdio transport and scaffolds `project-context/` coordination files if they don't exist.

## Event Observability

Every hook in this bundle emits structured JSONL events to `~/.mempalace/events/{session_id}.jsonl`. You can observe memory activity in real time:

```bash
tail -f ~/.mempalace/events/*.jsonl
```

Query events from within a session using the `palace events` operation:

```
# Last 10 events in the current session
palace(operation="events", limit=10)

# Tail events from the capture hook only
palace(operation="events", hook_filter="mempalace-capture", limit=20)

# All briefing events, oldest first
palace(operation="events", hook_filter="mempalace-briefing", tail=false, limit=50)
```

Each event has: `v` (schema version), `ts` (ISO-8601 UTC), `sid` (session ID), `hook`, `event`, `ok` (bool), `preview` (first ~100 chars of content), and `data` (structured payload). To disable event emission for a hook, set `emit_events: false` in `behaviors/mempalace.yaml`.

## Palace Gardening

The `palace garden` operation performs on-demand deep analysis of a palace wing. It is a background maintenance operation — run it manually when you want to understand the shape of your palace or backfill importance tags on pre-v1.2.0 drawers.

```
# Analyze the last 90 days of drawers in wing_myapp
palace(operation="garden", wing="wing_myapp")

# Scoped to a specific room
palace(operation="garden", wing="wing_myapp", room="auth-decisions", max_drawers=50)
```

Garden produces:
- **Cluster detection** — finds groups of related drawers via BFS over pairwise similarity and writes `part_of_cluster` / `has_label` KG edges.
- **Cross-room linking** — clusters spanning multiple rooms get a `spans_rooms` KG edge for later traversal.
- **Importance backfill** — drawers missing `has_importance` KG facts get scored via the Phase 3 rubric.
- **Diary entry** — the Curator diary records what was analyzed and how many clusters/edges were created.

Garden is bounded by `garden_max_drawers` (default 200, hard cap 500) and a 120-second wall-clock budget. It never deletes anything.
