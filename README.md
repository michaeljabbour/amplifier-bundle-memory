# amplifier-bundle-memory

A local-first, two-layer memory system for [Amplifier](https://github.com/microsoft/amplifier).

**Layer 1 — Semantic palace** ([MemPalace](https://github.com/MemPalace/mempalace)): verbatim storage with 96.6% R@5 retrieval on LongMemEval, knowledge graph, agent diaries, and cross-wing graph traversal. Nothing leaves your machine.

**Layer 2 — Coordination files** ([project-context](https://github.com/michaeljabbour/project-context)): structured Markdown files (`PROJECT_CONTEXT.md`, `HANDOFF.md`, `GLOSSARY.md`, etc.) that persist in the repo, survive clones, and are read natively by every AI coding tool.

---

## What's New in v1.2.0

Released 2026-04-17.

- **Event observability**: every hook emits structured events to `~/.mempalace/events/{session_id}.jsonl`. New `palace events` tool operation for querying. Kill switch per hook: `emit_events: false`.
- **Phase 3 curator**: at session:end, the curator now enriches the KG with `has_importance` (rubric-scored 0.0-1.0), `has_category`, `duplicates`, and `related_to` facts. Zero deletion — duplicates are preserved with low importance, not dropped.
- **Briefing re-ranking**: `final = semantic + weight * (importance - 0.5) * 0.08`. Max boost +/-0.04 at weight=1.0. Kill switch: `briefing_importance_weight: 0.0` -> identical to v1.1.0.
- **`palace garden` operation**: on-demand structural analysis (BFS clustering, KG edges, diary entry, importance backfill).
- **`mempalace:docent` agent**: conversational memory Q&A in natural language.
- **Research paper**: full design + evaluation writeup at [`docs/research/gene-transfer-v1.2.0.pdf`](docs/research/gene-transfer-v1.2.0.pdf).

---

## Session Lifecycle

```
session:start
  ├── hooks-mempalace-briefing  →  ephemeral briefing (palace + KG + diary + HANDOFF.md)
  │                                 with importance re-ranking (kill switch: briefing_importance_weight=0.0)
  └── hooks-project-context      →  inject Tier 1 coordination files; scaffold if missing

during work
  ├── hooks-mempalace-capture    →  verbatim palace drawers + emit `drawer_filed` event
  ├── hooks-mempalace-interject  →  surface relevant memory on prompt_submit/tool_pre/orchestrator_complete
  │                                 (only when cosine ≥ 0.72, LLM-judged when uncertain)
  └── (every hook)               →  emit events to ~/.mempalace/events/{session_id}.jsonl

session:end
  ├── hooks-project-context      →  delegates to Curator
  └── Curator agent
      ├── Phase 1: palace curation (verbatim drawers)
      ├── Phase 2: coordination file updates (HANDOFF.md, PROVENANCE.md, ...)
      └── Phase 3: KG enrichment (has_importance, has_category, duplicates, related_to)

on-demand
  ├── mempalace:archivist        →  precise read-path (palace search, KG queries)
  ├── mempalace:docent           →  conversational memory Q&A ("what did I work on last week?")
  ├── mempalace:curator          →  explicit remember / handoff update
  └── palace(operation="garden") →  deep clustering + importance backfill + diary entry
```

---

## Agents

| Agent | Trigger | Role |
|---|---|---|
| `mempalace:archivist` | on-demand | Precise read path: palace search, KG queries, graph traversal, coordination file reads |
| `mempalace:docent` | on-demand | **(New in v1.2.0)** Conversational memory Q&A — natural-language questions about history, decisions, patterns, session recap |
| `mempalace:curator` | session:end / on-demand | Write path: palace curation, Phase 3 KG enrichment, HANDOFF.md, PROVENANCE.md, GLOSSARY.md |

---

## Modules

| Module | Type | Description |
|---|---|---|
| `hooks-mempalace-briefing` | hook | Session-start briefing from palace + KG + diary + coordination files. Importance re-ranking (weight=1.0 default, 0.0 disables). Emits `briefing_assembled` / `briefing_skipped`. |
| `hooks-mempalace-capture` | hook | Verbatim palace capture on tool:post with category detection. Emits `drawer_filed` / `capture_skipped`. |
| `hooks-mempalace-interject` | hook | Mid-session memory surfacing (cosine >= 0.72, LLM-judged in uncertain band). Emits `memory_surfaced` / `interject_skipped`. |
| `hooks-project-context` | hook | Reads Tier 1 coordination files at session:start; delegates HANDOFF update at session:end. Emits `coordination_read` / `coordination_scaffolded` / `curator_delegated`. |
| `tool-mempalace` | tool | Palace operations: `search`, `remember`, `kg`, `traverse`, `diary`, `mine`, `events` *(new v1.2.0)*, `garden` *(new v1.2.0)*. Also hosts the shared event emitter. |
| `tool-memory` | tool | SQLite key-value fact store for explicit memories (composed from external repo). |

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
# 1. Install MemPalace (the semantic memory engine)
pip install mempalace

# 2. Initialize a palace for your project
mempalace init ~/projects/myapp

# 3. Add this bundle to Amplifier and make it active
# (Pin to the latest installable release — v1.2.1 is v1.2.0 + a packaging fix)
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-bundle-memory@v1.2.1
amplifier bundle use memory

# Optional: add to your always-on `app` bundles so memory composes into every session
# (Edit ~/.amplifier/settings.yaml → bundle.app → append the git URL)

# 4. Run — coordination files scaffold automatically on first session
amplifier run "start a session"
```

> **Note**: The bundle degrades gracefully without MemPalace installed (coordination-files-only mode), but palace search, KG, and garden features silently skip. Install MemPalace for the full experience.

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

### Query the session event log *(new in v1.2.0)*

```
palace(operation="events", hook_filter="mempalace-capture", limit=20, tail=True)
```

Returns structured events from `~/.mempalace/events/{session_id}.jsonl`. Filter by hook (`mempalace-capture`, `mempalace-briefing`, `mempalace-interject`, `project-context`, `tool-mempalace`) or event type (`drawer_filed`, `briefing_assembled`, `garden_completed`, etc.). Useful for debugging or live observability: `tail -f ~/.mempalace/events/*.jsonl`.

### Run palace garden (deep structural analysis) *(new in v1.2.0)*

```
palace(operation="garden", wing="wing_myapp", lookback_days=90, max_drawers=200)
```

On-demand BFS clustering of drawers in a wing. Produces:
- Cluster KG edges (`part_of_cluster`, `is_a`, `has_label`, `has_size`, `spans_rooms`)
- Curator diary entry summarizing the run
- Importance backfill for drawers missing `has_importance` KG facts (using the Phase 3 rubric)

Zero deletion — all outputs are additive KG facts. Bounded by `max_drawers` (hard cap 500) and a 120s total timeout.

### Ask natural-language questions *(new in v1.2.0)*

Delegate to the **docent agent** for conversational memory Q&A:

> "What decisions have I made about authentication?"
> "Summarize what I worked on last week."
> "Which patterns keep recurring across my projects?"

The docent synthesizes from palace search + KG + diaries + session events + coordination files.

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
| LongMemEval (hybrid + LLM rerank) | R@5 | >=99% |
| LoCoMo (hybrid v5, top-10) | R@10 | 88.9% |
| **Briefing re-ranking** (v1.2.0, 200x30 synthetic) | **R@5 delta** | **+0.022** (baseline 0.567 -> reranked 0.589) |

The first four rows are properties of MemPalace's retrieval engine. The last row measures v1.2.0's briefing hook re-ranking on a local synthetic proxy — the harness supports running against real LongMemEval when the dataset is available. Full methodology in `docs/research/gene-transfer-v1.2.0.pdf`.

See `evals/` for benchmark runner configuration and the `mempalace:evaluator` agent for running evals via Amplifier.

---

## Research Paper

Full architectural + evaluation writeup: [`docs/research/gene-transfer-v1.2.0.pdf`](docs/research/gene-transfer-v1.2.0.pdf) (14 pages, 5 Graphviz figures).

Covers: gene-transfer concept, system architecture, event observability design, KG intelligence (Phase 3 + briefing re-rank with formula proofs + palace garden), evaluation with benchmark methodology, philosophy preservation analysis, deferred work.

Rebuild from source: `cd docs/research && make all` (requires LaTeX + graphviz).

---

## Credits

- [MemPalace](https://github.com/MemPalace/mempalace) — local-first semantic memory engine
- [project-context](https://github.com/michaeljabbour/project-context) — coordination file system
- [Amplifier](https://github.com/microsoft/amplifier) — agent framework and bundle system

---

## License

MIT