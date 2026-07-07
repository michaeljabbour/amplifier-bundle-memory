# Memory System Skill

## Overview

This skill guides agents on how to use the native memory system effectively
within Amplifier. It provides local-first, verbatim AI memory with 96.6% R@5
retrieval accuracy on LongMemEval — no API key required, nothing leaves your
machine by default. Exception: hooks-memory-interject's optional
`llm_judge_enabled` config (off by default) sends query + memory text to
OpenAI when explicitly enabled.

## Quick Reference

### Filing a Memory

Use the `memory` tool with `operation: remember`:

```
memory(operation="remember", wing="wing_myapp", room="auth-migration",
       content="<verbatim text to store>")
```

**Always store verbatim content.** Do not summarize or paraphrase — the accuracy
comes from storing the original text.

### Searching Memory

```
memory(operation="search", query="why did we switch to GraphQL",
       wing="wing_myapp", limit=5)
```

Always pass `wing` and/or `room` filters when the context is clear. Unscoped
searches are slower and less precise.

### Knowledge Graph

```
# Query entity facts
memory(operation="kg", kg_action="query", entity="myapp")

# Add a fact
memory(operation="kg", kg_action="add",
       subject="myapp", predicate="uses", object="PostgreSQL")

# Invalidate a stale fact
memory(operation="kg", kg_action="invalidate",
       subject="myapp", predicate="uses", object="MySQL")
```

### Agent Diary

```
# Write a diary entry
memory(operation="diary", diary_action="write",
       agent_name="amplifier",
       entry="Decided to use Clerk for auth because...")

# Read recent entries
memory(operation="diary", diary_action="read", agent_name="amplifier", limit=5)
```

### Graph Traversal

```
# Find connected ideas across wings
memory(operation="traverse", start_room="auth-migration", max_hops=2)
```

## Memory Taxonomy

When filing drawers, use these room naming conventions for consistency:

| Category | Room Name Pattern | Example |
|---|---|---|
| Decisions | `decisions` | `decisions` |
| Architecture | `architecture` | `architecture` |
| Blockers | `blockers` | `blockers` |
| Resolved issues | `resolved-{topic}` | `resolved-auth-bug` |
| Dependencies | `dependencies` | `dependencies` |
| Patterns | `patterns` | `patterns` |
| Lessons learned | `lessons` | `lessons` |
| Feature work | `{feature-name}` | `auth-migration` |

## When to File Memories

File a memory when:
- A significant decision is made (architecture, technology choice, rejected alternative)
- A blocker is encountered or resolved
- A pattern or anti-pattern is discovered
- A lesson is learned from a debugging session
- An important fact about a person or project changes

Do **not** file:
- Trivial tool outputs (file listings, status checks)
- Content longer than 8KB (too noisy)
- Duplicate content (the store dedups content-addressed cells automatically)

## Setup

Nothing to install manually — the memory daemon auto-starts on first use
(the tool spawns it, discovers it via `~/.amplifier/memory/daemon.json`).
Durability requires the amplifier-data Rust kernel (a Rust toolchain is the
install-time prerequisite; see the bundle README).

## Migrating from a legacy vendor store

If you have an existing legacy vendor store (its default location is
documented in `amplifier-memory-import --help`):

```bash
pip install 'amplifier-module-tool-memory[migrate]'
amplifier-memory-import --verify
```

This copies drawers and their embeddings verbatim (same vector space) into
the native store. The source directory is never modified.

## `memory` operation `events` — Query the Event Log

Every hook writes structured JSONL events to
`~/.amplifier/memory/events/{session_id}.jsonl`. The `events` operation lets
you read them without leaving the session.

```
# Tail the last 10 events (default)
memory(operation="events")

# Last 50 events, showing oldest first
memory(operation="events", limit=50, tail=false)

# Capture-hook events only
memory(operation="events", hook_filter="memory-capture")

# Only drawer_filed events
memory(operation="events", event_filter="drawer_filed", limit=100)

# Briefing events for a specific session
memory(operation="events", session_id="abc123", hook_filter="memory-briefing")
```

**Response shape**: `{session_id, event_count, returned, skipped_lines, events[]}`. Each event has `hook`, `event`, `ok`, `preview`, `data`, `ts`.

**When to use**: Debugging why a memory was/wasn't captured. Verifying that briefings assembled correctly. Checking if the interject hook fired. Auditing session activity.

## `memory` operation `garden` — On-Demand Analysis

The `garden` operation performs deep analysis of a wing: detects content
clusters, backfills importance tags, and writes KG edges. Run it manually on
a mature memory store to enrich it.

```
# Analyze all drawers in wing_myapp (last 90 days, up to 200)
memory(operation="garden", wing="wing_myapp")

# Focused analysis of a specific room
memory(operation="garden", wing="wing_myapp", room="auth-decisions", max_drawers=50)

# Broader lookback window
memory(operation="garden", wing="wing_myapp", lookback_days=180)
```

**What garden produces**:
- `clusters` — groups of related drawers linked via `part_of_cluster` KG edges, each with a label, dominant category, and rooms spanned.
- `kg_edges_created` — total KG triples written (`is_a`, `has_label`, `has_size`, `part_of_cluster`, optionally `spans_rooms`).
- `importance_backfilled` — drawers that had no `has_importance` fact now have one (Phase 3 rubric applied).
- `diary_entry` — "written" confirms a Curator diary entry was created.

**When to run**: Occasionally — not every session. Useful after large import batches, after several weeks of active use, or when you want to discover content clusters before a refactor. Bounded by `max_drawers` (default 200) and a 120-second wall-clock budget.
