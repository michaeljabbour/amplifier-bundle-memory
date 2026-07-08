# amplifier-bundle-memory

A local-first, two-layer memory system for [Amplifier](https://github.com/microsoft/amplifier).

**Layer 1 — Native semantic memory**: verbatim storage (via the amplifier-data substrate and an auto-started local memory daemon) with 96.6% R@5 retrieval on LongMemEval, a knowledge graph, agent diaries, and cross-wing graph traversal. Nothing leaves your machine by default (local embedding model). The one opt-in exception: hooks-memory-interject's `llm_judge_enabled` (off by default) sends query + memory text to OpenAI for borderline-relevance scoring -- see modules/hooks-memory-interject/README.md.

**Layer 2 — Coordination files** ([project-context](https://github.com/michaeljabbour/project-context)): structured Markdown files (`PROJECT_CONTEXT.md`, `HANDOFF.md`, `GLOSSARY.md`, etc.) that persist in the repo, survive clones, and are read natively by every AI coding tool.

---

## Part of the behavioral-plasticity suite

This repo is one component of the **behavioral-plasticity suite**, composed by the conductor bundle [`amplifier-bundle-behavioral-plasticity`](https://github.com/michaeljabbour/amplifier-bundle-behavioral-plasticity) (memory + the amplifier-data substrate + the context-intelligence survey/scoring pieces + a falsification harness). Installing that one bundle pulls in this repo automatically.

**Install the full suite (always-on):**
```bash
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-bundle-behavioral-plasticity@main --app
amplifier bundle update behavioral-plasticity -y
```

**Test:**
```bash
amplifier run --mode single "List your tools, then call falsification_harness once and print its JSON."
```
Passing: tools include `falsification_harness` + the memory tools (`memory`, `add_memory`, …); JSON shows `"verdict": "proxy"`, `"n_probes": 50`, `"lift": ~0.17`, `"success": true`. `proxy` is the expected correct result, not a failure. First compose is slow (pulls the included bundles and compiles amplifier-data's native Rust/PyO3 component); cached after. Remove with `amplifier bundle remove behavioral-plasticity`.

---

## What's New in v2.0.0

**Breaking: native cutover.** The prior vendor-backed (ChromaDB) store is
gone. Memory is now backed entirely by amplifier-data through an
auto-started local memory daemon (a local ONNX embedder, no torch, no
external network calls by default). The tool (previously named after the vendor) is now named `memory`
(operations unchanged); the standalone SQLite fact-store module formerly
registered under the name `tool-memory` is dropped (its niche is covered by
the native `kg` operation). See `CHANGELOG.md` for full migration
instructions -- existing data migrates via `amplifier-memory-import`.

## What's New in v1.2.0

Released 2026-04-17.

- **Event observability**: every hook emits structured events to `~/.amplifier/memory/events/{session_id}.jsonl`. New `memory events` tool operation for querying. Kill switch per hook: `emit_events: false`.
- **Phase 3 curator**: at session:end, the curator now enriches the KG with `has_importance` (rubric-scored 0.0-1.0), `has_category`, `duplicates`, and `related_to` facts. Zero deletion — duplicates are preserved with low importance, not dropped.
- **Briefing re-ranking**: `final = semantic + weight * (importance - 0.5) * 0.08`. Max boost +/-0.04 at weight=1.0. Kill switch: `briefing_importance_weight: 0.0` -> identical to v1.1.0.
- **`memory garden` operation**: on-demand structural analysis (BFS clustering, KG edges, diary entry, importance backfill).
- **`memory:docent` agent**: conversational memory Q&A in natural language.
- **Research paper**: full design + evaluation writeup at [`docs/research/gene-transfer-v1.2.0.pdf`](docs/research/gene-transfer-v1.2.0.pdf).

---

## Session Lifecycle

```
session:start
  ├── hooks-memory-briefing  →  ephemeral briefing (memory search + KG + diary + HANDOFF.md)
  │                                 with importance re-ranking (kill switch: briefing_importance_weight=0.0)
  └── hooks-project-context      →  inject Tier 1 coordination files; scaffold if missing

during work
  ├── hooks-memory-capture    →  verbatim memory drawers + emit `drawer_filed` event
  ├── hooks-memory-interject  →  surface relevant memory on prompt_submit/tool_pre/orchestrator_complete
  │                                 (only when cosine >= 0.72, LLM-judged when uncertain)
  └── (every hook)               →  emit events to ~/.amplifier/memory/events/{session_id}.jsonl

session:end
  ├── hooks-project-context      →  delegates to Curator
  └── Curator agent
      ├── Phase 1: memory curation (verbatim drawers)
      ├── Phase 2: coordination file updates (HANDOFF.md, PROVENANCE.md, ...)
      └── Phase 3: KG enrichment (has_importance, has_category, duplicates, related_to)

on-demand
  ├── memory:archivist        →  precise read-path (memory search, KG queries)
  ├── memory:docent           →  conversational memory Q&A ("what did I work on last week?")
  ├── memory:curator          →  explicit remember / handoff update
  └── memory(operation="garden") →  deep clustering + importance backfill + diary entry
```

---

## Agents

| Agent | Trigger | Role |
|---|---|---|
| `memory:archivist` | on-demand | Precise read path: memory search, KG queries, graph traversal, coordination file reads |
| `memory:docent` | on-demand | **(New in v1.2.0)** Conversational memory Q&A — natural-language questions about history, decisions, patterns, session recap |
| `memory:curator` | session:end / on-demand | Write path: memory curation, Phase 3 KG enrichment, HANDOFF.md, PROVENANCE.md, GLOSSARY.md |

---

## Modules

| Module | Type | Description |
|---|---|---|
| `hooks-memory-briefing` | hook | Session-start briefing from memory + KG + diary + coordination files. Importance re-ranking (weight=1.0 default, 0.0 disables). Emits `briefing_assembled` / `briefing_skipped`. |
| `hooks-memory-capture` | hook | Verbatim memory capture on tool:post with category detection. Emits `drawer_filed` / `capture_skipped`. |
| `hooks-memory-interject` | hook | Mid-session memory surfacing (cosine >= 0.72, LLM-judged in uncertain band). Emits `memory_surfaced` / `interject_skipped`. |
| `hooks-project-context` | hook | Reads Tier 1 coordination files at session:start; delegates HANDOFF update at session:end. Emits `coordination_read` / `coordination_scaffolded` / `curator_delegated`. |
| `tool-memory` | tool | Native memory operations: `search`, `remember`, `kg`, `traverse`, `diary`, `mine`, `events`, `garden`. Also hosts the shared event emitter and the memory daemon. |

> **v2.0.0 note**: the standalone SQLite fact-store module previously listed here (also named `tool-memory`, composed from an external repo) is DROPPED — see "What's New in v2.0.0" above.

---

## Deduplication

This bundle consolidates five previously separate modules:

| Superseded | Replaced By |
|---|---|
| `amplifier-bundle-memory` | `behaviors/memory.yaml` |
| `amplifier-bundle-project-memory` | `behaviors/memory.yaml` |
| `amplifier-module-context-memory` | `hooks-memory-briefing` |
| `amplifier-module-tool-memory` (SQLite fact store) | `tool-memory`'s native `kg` operation (v2.0.0) |
| `amplifier-module-hooks-memory-capture` | `hooks-memory-capture` (category detection built in) |

---

## Setup

```bash
# 1. Add this bundle to Amplifier and make it active
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-bundle-memory@main
amplifier bundle use memory

# Optional: add to your always-on `app` bundles so memory composes into every session
# (Edit ~/.amplifier/settings.yaml → bundle.app → append the git URL)

# 2. Run — the memory daemon auto-starts on first use. Nothing else to
#    install. project-context coordination-file scaffolding is disabled by
#    default (see "project-context Coordination Files" below).
amplifier run "start a session"
```

> **Note**: durable storage requires the amplifier-data Rust kernel (a Rust
> toolchain is the install-time prerequisite; installing this bundle's
> pinned amplifier-data git dependency builds it automatically via maturin).

> **Migrating existing data** from a pre-2.0.0 install: see "Migrating from a legacy vendor store" in `skills/memory/SKILL.md`, or run `amplifier-memory-import --verify` after installing the `[migrate]` extra.

---

## Usage

### Search Memory

```
memory(operation="search", query="why did we switch to GraphQL", wing="wing_myapp")
```

### File a Memory

```
memory(operation="remember", wing="wing_myapp", room="decisions",
       content="Decided to use Clerk for auth because Auth0 pricing changed.")
```

### Knowledge Graph

```
memory(operation="kg", kg_action="add",
       subject="myapp", predicate="uses", object="PostgreSQL")
```

### Agent Diary

```
memory(operation="diary", diary_action="write", agent_name="amplifier",
       entry="Resolved the N+1 query issue by adding DataLoader.")
```

### Query the session event log

```
memory(operation="events", hook_filter="memory-capture", limit=20, tail=True)
```

Returns structured events from `~/.amplifier/memory/events/{session_id}.jsonl`. Filter by hook (`memory-capture`, `memory-briefing`, `memory-interject`, `project-context`, `tool-memory`) or event type (`drawer_filed`, `briefing_assembled`, `garden_completed`, etc.). Useful for debugging or live observability: `tail -f ~/.amplifier/memory/events/*.jsonl`.

### Run memory garden (deep structural analysis)

```
memory(operation="garden", wing="wing_myapp", lookback_days=90, max_drawers=200)
```

On-demand BFS clustering of drawers in a wing. Produces:
- Cluster KG edges (`part_of_cluster`, `is_a`, `has_label`, `has_size`, `spans_rooms`)
- Curator diary entry summarizing the run
- Importance backfill for drawers missing `has_importance` KG facts (using the Phase 3 rubric)

Zero deletion — all outputs are additive KG facts. Bounded by `max_drawers` (hard cap 500) and a 120s total timeout.

### Ask natural-language questions

Delegate to the **docent agent** for conversational memory Q&A:

> "What decisions have I made about authentication?"
> "Summarize what I worked on last week."
> "Which patterns keep recurring across my projects?"

The docent synthesizes from memory search + KG + diaries + session events + coordination files.

---

## project-context Coordination Files

Auto-scaffolding is **disabled by default** (`setup_if_missing: false` in `behaviors/memory.yaml`): the `hooks-project-context` hook reads and updates an existing `project-context/` directory but will not create one in projects that lack it. To scaffold a project deliberately:

```bash
amplifier run "set up project-context coordination files for this project"
```

Or set `setup_if_missing: true` in `behaviors/memory.yaml` to restore automatic scaffolding everywhere.

Once present, these files (plus `AGENTS.md` at the project root) are cross-platform — read natively by Amplifier, OpenAI Codex, GitHub Copilot, Cursor, and Windsurf.

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

The first four rows are properties of the retrieval engine. The last row measures the briefing hook's re-ranking on a local synthetic proxy — the harness supports running against real LongMemEval when the dataset is available. Full methodology in `docs/research/gene-transfer-v1.2.0.pdf`.

The benchmark runner lives in `tests/test_benchmark_recall.py` (run the full R@5 simulation with `pytest -m benchmark`); raw run logs backing the re-ranking delta above are in `docs/eval/briefing-rerank-benchmark.md`. The LongMemEval/LoCoMo evaluation methodology is documented in `docs/eval/EVALUATION.md`.

---

## Research Paper

Full architectural + evaluation writeup: [`docs/research/gene-transfer-v1.2.0.pdf`](docs/research/gene-transfer-v1.2.0.pdf) (14 pages, 5 Graphviz figures).

Covers: gene-transfer concept, system architecture, event observability design, KG intelligence (Phase 3 + briefing re-rank with formula proofs + memory garden), evaluation with benchmark methodology, philosophy preservation analysis, deferred work.

Rebuild from source: `cd docs/research && make all` (requires LaTeX + graphviz).

---

## Credits

- [project-context](https://github.com/michaeljabbour/project-context) — coordination file system
- [Amplifier](https://github.com/microsoft/amplifier) — agent framework and bundle system
- Built on the shoulders of open-source memory research (see `docs/research/`)

---

## Development

For end-to-end testing and bundle development, a [Digital Twin Universe (DTU) profile](docs/development/dtu.md) is provided.

See [docs/development/dtu.md](docs/development/dtu.md) for:
- Prerequisites and setup
- Launching the test environment
- Running integration tests
- Interactive session testing
- The update loop for iterating on changes

---

## License

MIT
