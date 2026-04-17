# amplifier-bundle-memory

A local-first, two-layer memory system for [Amplifier](https://github.com/microsoft/amplifier).

**Layer 1 — Semantic palace** ([MemPalace](https://github.com/MemPalace/mempalace)): verbatim storage with 96.6% R@5 retrieval on LongMemEval, knowledge graph, agent diaries, and cross-wing graph traversal. Nothing leaves your machine.

**Layer 2 — Coordination files** ([project-context](https://github.com/michaeljabbour/project-context)): structured Markdown files (`PROJECT_CONTEXT.md`, `HANDOFF.md`, `GLOSSARY.md`, etc.) that persist in the repo, survive clones, and are read natively by every AI coding tool.

---

## Session Lifecycle

```
session:start
  └── hooks-mempalace-briefing  →  ephemeral briefing (palace + KG + diary + HANDOFF.md)
  └── hooks-project-context     →  inject Tier 1 coordination files; scaffold if missing

during work
  └── hooks-mempalace-capture   →  verbatim palace drawers (auto-wing, auto-room, category-tagged)

session:end
  └── hooks-project-context     →  delegates to Curator
  └── Curator agent             →  palace curation + HANDOFF.md + PROVENANCE.md + GLOSSARY.md
```

---

## Agents

| Agent | Trigger | Role |
|---|---|---|
| `mempalace:archivist` | on-demand | Read path: palace search, KG queries, graph traversal, coordination file reads |
| `mempalace:curator` | session:end / on-demand | Write path: palace curation, KG updates, HANDOFF.md, PROVENANCE.md, GLOSSARY.md |

---

## Modules

| Module | Type | Description |
|---|---|---|
| `hooks-mempalace-briefing` | hook | Session-start briefing from palace + KG + diary + coordination files |
| `hooks-mempalace-capture` | hook | Verbatim palace capture on tool:post (absorbs heuristic category detection) |
| `hooks-project-context` | hook | Reads coordination files at start; delegates HANDOFF update at end |
| `tool-mempalace` | tool | High-level palace tool: search, remember, kg, traverse, diary, mine |
| `tool-memory` | tool | SQLite key-value fact store for explicit memories |

---

## Deduplication

This bundle consolidates five previously separate modules:

| Superseded | Replaced By |
|---|---|
| `amplifier-bundle-memory` | `behaviors/mempalace.yaml` |
| `amplifier-bundle-project-memory` | `behaviors/mempalace.yaml` |
| `amplifier-module-context-memory` | `hooks-mempalace-briefing` |
| `amplifier-module-tool-memory` | `tool-mempalace` + `tool-memory` |
| `amplifier-module-hooks-memory-capture` | `hooks-mempalace-capture` (category detection built in) |

---

## Setup

```bash
# 1. Install MemPalace
pip install mempalace

# 2. Initialize a palace for your project
mempalace init ~/projects/myapp

# 3. Add this bundle to your Amplifier app bundle
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-bundle-memory@main

# 4. Run — coordination files are scaffolded automatically on first session
amplifier run "start a session"
```

---

## Usage

### Search Memory

```
palace(operation="search", query="why did we switch to GraphQL", wing="wing_myapp")
```

### File a Memory

```
palace(operation="remember", wing="wing_myapp", room="decisions",
       content="Decided to use Clerk for auth because Auth0 pricing changed.")
```

### Knowledge Graph

```
palace(operation="kg", kg_action="add",
       subject="myapp", predicate="uses", object="PostgreSQL")
```

### Agent Diary

```
palace(operation="diary", diary_action="write", agent_name="amplifier",
       entry="Resolved the N+1 query issue by adding DataLoader.")
```

---

## project-context Coordination Files

On first run, the bundle scaffolds a `project-context/` directory and an `AGENTS.md` at the project root. These files are cross-platform — read natively by Amplifier, OpenAI Codex, GitHub Copilot, Cursor, and Windsurf.

| File | Tier | Purpose |
|---|---|---|
| `AGENTS.md` | — | Cross-platform agent entry point (project root) |
| `project-context/PROJECT_CONTEXT.md` | 1 | Current phase, milestone, team |
| `project-context/GLOSSARY.md` | 1 | Canonical terminology |
| `project-context/HANDOFF.md` | 1 | Last session summary and next steps |
| `project-context/STRUCTURE.md` | 2 | Directory layout |
| `project-context/WAYSOFWORKING.md` | 2 | Proven workflows and failure patterns |
| `project-context/PROVENANCE.md` | 2 | Decision log |
| `project-context/EXPERIMENT_JOURNAL.md` | 2 | Experiment results and benchmarks |

---

## Benchmarks

| Benchmark | Metric | Score |
|---|---|---|
| LongMemEval (raw, no LLM) | R@5 | **96.6%** |
| LongMemEval (hybrid v4, held-out) | R@5 | **98.4%** |
| LongMemEval (hybrid + LLM rerank) | R@5 | ≥99% |
| LoCoMo (hybrid v5, top-10) | R@10 | 88.9% |

See `evals/` for benchmark runner configuration and the `mempalace:evaluator` agent for running evals via Amplifier.

---

## Credits

- [MemPalace](https://github.com/MemPalace/mempalace) — local-first semantic memory engine
- [project-context](https://github.com/michaeljabbour/project-context) — coordination file system
- [Amplifier](https://github.com/microsoft/amplifier) — agent framework and bundle system

---

## License

MIT
