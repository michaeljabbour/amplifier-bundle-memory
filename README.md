# amplifier-bundle-memory

A local-first, three-layer memory system for [Amplifier](https://github.com/microsoft/amplifier), plus [project-context](https://github.com/michaeljabbour/project-context) coordination files.

**L1 — Evidence (drawers)**: verbatim storage (via the amplifier-data substrate and an auto-started local memory daemon), content-addressed, with `filed_at`/`in_session`/`at_commit` provenance. Ground truth -- always cited, never paraphrased away.

**L2 — Facts**: 15-80 word derived statements written by the `memory:distiller` agent, each required to cite >=1 source drawer (`@memory:derived_from`) -- add-only, supersede-never-delete. Auditable against the verbatim evidence that produced them.

**L3 — Index**: room/wing abstracts + standing question/answer pairs, computed deterministically (mechanism-first, D19) so they're always available without an LLM; a curator agent can supersede the derived view with better prose, which is served until enough new facts/drawers accumulate (`pending_changes > 10`) that it falls back to the fresh derived view.

Retrieval is hybrid (semantic + BM25, fused by RRF) across L1+L2 in one ranked list, with each hit's `layer` and per-arm rank reported for explainability. Nothing leaves your machine by default (local embedding model). The one opt-in exception: hooks-memory-interject's `llm_judge_enabled` (off by default) sends query + memory text to OpenAI for borderline-relevance scoring -- see modules/hooks-memory-interject/README.md.

**Coordination files** ([project-context](https://github.com/michaeljabbour/project-context)): structured Markdown files (`PROJECT_CONTEXT.md`, `HANDOFF.md`, `GLOSSARY.md`, etc.) that persist in the repo, survive clones, and are read natively by every AI coding tool.

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

## What's New in v2.1.0

Three-layer memory (design: `docs/plans/2026-09-27-memory-layers-design.md`).
See `CHANGELOG.md` for the full list; highlights:

- **L2 facts** and **L3 index + standing questions** (new tool ops: `fact_add`,
  `facts`, `index`, `index_set`, `standing_add`, `standing_answer`, `standing`).
- **Layered briefing** (Memory map -> Standing answers -> Known facts ->
  Relevant evidence) is now the default session-start briefing; legacy stays
  available and byte-identical for daemons that predate the L3 ops.
- **Hybrid RRF search** (semantic + BM25) with `since`/`until` temporal
  filters; `fusion="legacy"` reproduces v2.0.1 exactly.
- **Opt-in cold-path reflection** (`behaviors/memory-reflect.yaml`,
  `behaviors/memory-index.yaml`): background distillation of conversation
  spans into facts, and curator index rollups -- both run as spawned agent
  sessions, never on the hot (in-turn) path, and both degrade to "queue only"
  when the app layer doesn't register `session.spawn`.
- **`memory:retrieved` / `memory:injected` events** (T4.1): every retrieval
  and every context injection is now observable, with stable content-address
  refs a downstream conductor can join against. See "Hot path vs. cold path"
  and "Event Observability" below.

### Hot path vs. cold path

| | Hot path (every turn) | Cold path (opt-in, background) |
|---|---|---|
| What runs | `hooks-memory-capture` (file), `hooks-memory-interject` (surface), `hooks-memory-briefing` (session:start), `tool-memory` ops | `hooks-memory-reflect` (distill spans into facts), curator (index rollup) |
| LLM calls | Zero (local embedder + deterministic reads only) | Yes -- one spawned agent session per distillation/rollup |
| Behavior | Always on (`behaviors/memory.yaml`) | Opt-in (`behaviors/memory-reflect.yaml`, `behaviors/memory-index.yaml`) |
| Failure mode | Fails open (never blocks the turn) | No-op / stays queued (no `session.spawn` capability -> job stays pending, never an error) |

### Running the AMB benchmark

`benchmarks/amb/` adapts this store to the external Agent Memory
Benchmark (AMB is a dev-only external tool -- nothing from it is vendored
into this repo; see `benchmarks/amb/README.md` for the project link and
install instructions). Offline smoke test (no AMB install, no
network): `pytest benchmarks/amb/tests/`. A real run against AMB's
LongMemEval-S / LoCoMo / PrecisionMemBench splits requires installing AMB
separately -- see `benchmarks/amb/README.md` for setup and `run.py` usage.

**Note on the 96.6% R@5 figure below**: it is retrieval-only (does the
right drawer come back in the top 5?), measured on a local synthetic/
LongMemEval-derived harness -- it is **not** comparable to AMB's or other
tools' judged QA-accuracy numbers (LLM-graded answer correctness), which
measure a different, strictly harder task. The measurement bar for "best of
all the tools" (`project-context/PROVENANCE.md` D13) is AMB QA accuracy and
PrecisionMemBench R@10 under identical AMB judge/answer models -- proposed,
pending ratification, not yet run against a live AMB install with API keys.

## What's New in v2.0.0

**Breaking: native cutover.** The prior externally-backed store is
gone. Memory is now backed entirely by amplifier-data through an
auto-started local memory daemon (a local ONNX embedder, no torch, no
external network calls by default). The tool (previously named after the prior
implementation) is now named `memory` (operations unchanged); the
standalone SQLite fact-store module formerly registered under the name
`tool-memory` is dropped (its niche is covered by the native `kg`
operation). See `CHANGELOG.md` for full history of the rename.

## What's New in v1.2.0

Released 2026-04-17.

- **Event observability**: every hook emits structured events to `~/.amplifier/memory/events/{session_id}.jsonl`. New `memory events` tool operation for querying. Kill switch per hook: `emit_events: false`.
- **Phase 3 curator**: at session:end, the curator now enriches the KG with `has_importance` (rubric-scored 0.0-1.0), `has_category`, `duplicates`, and `related_to` facts. Zero deletion — duplicates are preserved with low importance, not dropped.
- **Briefing re-ranking**: `final = semantic + weight * (importance - 0.5) * 0.08`. Max boost +/-0.04 at weight=1.0. Kill switch: `briefing_importance_weight: 0.0` -> identical to v1.1.0.
- **`memory garden` operation**: on-demand structural analysis (BFS clustering, KG edges, diary entry, importance backfill).
- **`memory:docent` agent**: conversational memory Q&A in natural language.

---

## Session Lifecycle

```
session:start
  ├── hooks-memory-briefing  →  ephemeral layered briefing (Memory map → Standing answers →
  │                                 Known facts → Relevant evidence); legacy mode available
  ├── hooks-project-context      →  inject Tier 1 coordination files; scaffold if missing
  └── hooks-memory-reflect (opt-in) → drain up to N pending reflection jobs in the background
                                    → (opt-in, memory-index) curator refresh if index is stale

during work (hot path — zero LLM calls)
  ├── hooks-memory-capture    →  verbatim memory drawers + emit `drawer_filed`, `memory:retrieved` events
  ├── hooks-memory-interject  →  surface relevant memory on prompt_submit/orchestrator_complete
  │                                 (tool_pre retrieval is available as an opt-in)
  │                                 (only when cosine >= 0.72, LLM-judged when uncertain)
  │                                 emits `memory:retrieved` (every attempt) + `memory:injected` (on inject)
  └── (every hook)               →  emit events to ~/.amplifier/memory/events/{session_id}.jsonl

context:compaction / context:pre_compact (cold path, opt-in — memory-reflect behavior)
  └── hooks-memory-reflect       →  watermark span → durable reflection_job cell
      └── memory:distiller (spawned agent) → fact_add / fact_supersede from the rubric

session:end
  ├── hooks-project-context      →  delegates to Curator
  ├── hooks-memory-reflect (opt-in) → queue final span as a reflection_job (never spawns, never blocks exit)
  └── Curator agent
      ├── Phase 1: memory curation (verbatim drawers)
      ├── Phase 2: coordination file updates (HANDOFF.md, PROVENANCE.md, ...)
      ├── Phase 3: KG enrichment (has_importance, has_category, duplicates, related_to)
      └── (opt-in, memory-index behavior) L3 index rollup + standing-question refresh

on-demand
  ├── memory:archivist        →  precise read-path (memory search, KG queries)
  ├── memory:docent           →  conversational memory Q&A ("what did I work on last week?")
  ├── memory:curator          →  explicit remember / handoff update / index rollup
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
| `hooks-memory-briefing` | hook | Session-start briefing: layered (Memory map/Standing answers/Known facts/Relevant evidence, default) or legacy (memory + KG + diary + coordination files, importance re-ranking). Emits `briefing_assembled` / `briefing_skipped` / `memory:retrieved` / `memory:injected`. |
| `hooks-memory-capture` | hook | Verbatim memory capture on tool:post with category detection, secret redaction, `filed_at`/`in_session`/`at_commit` provenance. Emits `drawer_filed` / `capture_skipped`. |
| `hooks-memory-interject` | hook | Mid-session memory surfacing (cosine >= 0.72, LLM-judged in uncertain band). Emits `memory_surfaced` / `interject_skipped` / `memory:retrieved` (every attempt) / `memory:injected` (on inject). |
| `hooks-memory-reflect` | hook (opt-in) | Cold-path: watermarks the conversation span since the last reflection on compaction/session:end, files it as a durable `reflection_job`, spawns `memory:distiller` (background agent) to write L2 facts. Ships in `behaviors/memory-reflect.yaml`, not the core behavior. |
| `hooks-project-context` | hook | Reads Tier 1 coordination files at session:start; delegates HANDOFF update at session:end. Emits `coordination_read` / `coordination_scaffolded` / `curator_delegated`. |
| `tool-memory` | tool | Native memory operations: `search` (hybrid RRF + `since`/`until`), `remember`, `kg`, `traverse`, `diary`, `mine`, `events`, `garden`, `fact_add`/`facts`, `reflection_job_add`/`reflection_jobs`/`reflection_job_done`, `index`/`index_set`, `standing_add`/`standing_answer`/`standing`. Also hosts the shared event emitter and the memory daemon. |

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

The first four rows are properties of the retrieval engine. The last row measures the briefing hook's re-ranking on a local synthetic proxy — the harness supports running against real LongMemEval when the dataset is available.

**These R@5/R@10 numbers are retrieval-only and not comparable to judged QA-accuracy numbers** (LLM-graded answer correctness, e.g. AMB's LongMemEval-S/LoCoMo scores) — see "Running the AMB benchmark" above and `project-context/PROVENANCE.md` D13 for the actual head-to-head measurement bar and its status.

The benchmark runner lives in `tests/test_benchmark_recall.py` (run the full R@5 simulation with `pytest -m benchmark`); raw run logs backing the re-ranking delta above are in `docs/eval/briefing-rerank-benchmark.md`. The LongMemEval/LoCoMo evaluation methodology is documented in `docs/eval/EVALUATION.md`.

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
