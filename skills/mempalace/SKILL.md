# MemPalace Memory System Skill

## Overview

This skill guides agents on how to use the MemPalace memory system effectively within Amplifier. MemPalace provides local-first, verbatim AI memory with 96.6% R@5 retrieval accuracy on LongMemEval — no API key required, nothing leaves your machine.

## Quick Reference

### Filing a Memory

Use the `palace` tool with `operation: remember`:

```
palace(operation="remember", wing="wing_myapp", room="auth-migration",
       content="<verbatim text to store>")
```

**Always store verbatim content.** Do not summarize or paraphrase — MemPalace's accuracy comes from storing the original text.

### Searching Memory

```
palace(operation="search", query="why did we switch to GraphQL",
       wing="wing_myapp", limit=5)
```

Always pass `wing` and/or `room` filters when the context is clear. Unscoped searches are slower and less precise.

### Knowledge Graph

```
# Query entity facts
palace(operation="kg", kg_action="query", entity="myapp")

# Add a fact
palace(operation="kg", kg_action="add",
       subject="myapp", predicate="uses", object="PostgreSQL")

# Invalidate a stale fact
palace(operation="kg", kg_action="invalidate",
       subject="myapp", predicate="uses", object="MySQL")
```

### Agent Diary

```
# Write a diary entry
palace(operation="diary", diary_action="write",
       agent_name="amplifier",
       entry="Decided to use Clerk for auth because...")

# Read recent entries
palace(operation="diary", diary_action="read", agent_name="amplifier", limit=5)
```

### Graph Traversal

```
# Find connected ideas across wings
palace(operation="traverse", start_room="auth-migration", max_hops=2)
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
- Duplicate content (always check with `mempalace_check_duplicate` first)

## Setup

```bash
pip install mempalace
mempalace init ~/projects/myapp
```

The MCP server starts automatically when this bundle is active.
