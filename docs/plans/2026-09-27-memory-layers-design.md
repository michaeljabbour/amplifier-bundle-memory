# Design: three-layer memory, Amplifier-shaped (v2.1 → v3.0)

**Status:** P0–P5 built on `feat/memory-layers` (2026-09-28, D16–D21); T1.5 + T4.2 + AMB baseline open. Direction accepted by Michael 2026-09-27; per-phase detail is
plan-ready. Evidence: `docs/research/2026-09-27-memory-systems-gene-survey.md`.
Decisions: `project-context/PROVENANCE.md` D6–D15. Each task below carries an
ID, files, interface and a machine-checkable acceptance so a plan-writing
session can expand it without re-deriving intent.

## 1. Owner's constraints (verbatim intent → rule)

| Source words (2026-09-27) | Rule |
|---|---|
| "amplifier shaped and works the best of all of the tools" | Every piece maps to a kernel module type / behavior / agent / context file per foundation docs; "best" is proven by the measurement bar (§6), not asserted |
| "gene transfer whatever you need" | Any mechanism from the ten surveyed repos is fair game |
| "don't vendor anything" | Reimplement; no copied code or prompts; AMB run as external tool; OpenViking (AGPL) and Zep paper (NC-SA) are ideas-only |
| "ok using amplifier-data or something else … upgrade amplifier-data too" | Substrate stays amplifier-data; new lenses land upstream there; pin bumps are explicit commits |
| "compaction should follow standard amplifier protocols and shape" | Reflection observes the standard context-manager events; memory ships **no** context manager |
| "build the missing layers … document everything clearly in small commits in the provenance log" | One decision per PROVENANCE entry; one concern per commit |

## 2. Architecture

```
            ┌──────────────── HOT PATH (no LLM, bounded latency) ────────────────┐
tool:post → hooks-memory-capture ─scrub→ L1 drawer (+filed_at, session, git_sha)  │
prompt    → hooks-memory-interject ──→ hybrid search (L2 ∪ L1) ── ephemeral inject│
session:start → hooks-memory-briefing → L3 index + standing answers + top facts   │
            └─────────────────────────────────────────────────────────────────────┘
            ┌──────────────── COLD PATH (LLM, background, opt-in behavior) ───────┐
context:compaction | context:pre_compact | session:end                           │
   → hooks-memory-reflect: watermark span → durable reflection-job cell          │
   → session.spawn(agent="memory:distiller", model_role [fast, general])         │
        distiller reads span + rubric → memory tool ops fact_add / fact_supersede│
   → curator (session:end, same spawn path): L3 index rollup, standing answers   │
            └─────────────────────────────────────────────────────────────────────┘
substrate: amplifier-data (cells, facts, lenses: vector, temporal, bm25[new])
```

### Layers

| Layer | Unit | Truth status | Written by | Read by |
|---|---|---|---|---|
| L1 Evidence | drawer (verbatim cell) | ground truth | capture hook, `remember` | search (fallback + citation) |
| L2 Facts | fact cell, 15–80 words, `@memory:derived_from` → ≥1 drawer | derived, auditable | distiller agent via tool ops | search (primary), briefing |
| L3 Index | room/wing abstract (≤256 chars) + overview (≤4000), standing Q&A | navigation only, never cited as evidence | curator via tool ops | briefing, two-stage search |

### Data model (all as amplifier-data facts on content-addressed cells; D8)

- Drawer facts: `filed_at` (ISO-8601 UTC), `in_session`, `at_commit` (git SHA, when in a repo).
- Fact cell payload: `{"kind":"fact","text":…,"fact_type":"world|experience|preference|procedure|correction","valid_at":…|null,"invalid_at":…|null}`; facts: `recorded_at`, `@memory:derived_from` (one per source drawer; `proof_count` = count), scope edges (wing/room).
- Supersede: `@memory:supersedes` edge new→old + `invalidate_fact(old, "@memory:current")` + `expired_at` fact on old. Never delete (substrate invariant).
- Contradiction that cannot be auto-resolved: amplifier-data `integrity` tension cell with both parties.
- Single-valued predicates (e.g. `package_manager`, `test_runner`, `default_branch`): newest wins deterministically, no LLM.
- Index cells: `{"kind":"abstract"|"overview","scope":"room:x"}` + `built_from_count`, `pending_changes`.
- Standing question: question cell + answer cell with `@memory:cites` → facts; stale iff any cited fact is no longer current.

### Retrieval (D9)

Arms over the scoped candidate set: semantic (existing VectorLens), BM25 (new
amplifier-data lens), temporal filter (range over `filed_at`/`valid_at`).
Fuse by RRF (k=60), per-arm cap, facts and drawers in one list with a fact
boost; importance/usage adjustments stay bounded (existing ±0.04 rule).
Each hit returns `layer`, `arms` (rank per arm) — explainable ranking.

## 3. Amplifier shape (D7, D10)

| Piece | Kernel/foundation type | Ships in |
|---|---|---|
| filed_at, scrub, facts ops, hybrid search, index ops | tool module internals + ops (`tool-memory`) | `behaviors/memory.yaml` (core) |
| BM25 lens, temporal range | amplifier-data library (upstream) | pin bump |
| `hooks-memory-reflect` | hook module | `behaviors/memory-reflect.yaml` (opt-in) |
| `memory:distiller` | agent (`agents/distiller.md`, `model_role: [fast, general]`) | memory-reflect behavior |
| reflection rubric | context file `context/reflection-rubric.md`, @-mentioned by distiller | memory-reflect behavior |
| index rollup, standing Q | curator agent work + tool ops | `behaviors/memory-index.yaml` (opt-in) |
| AMB adapter | dev tooling `benchmarks/amb/` (not a module) | repo only |
| new events | registered via `observability.events` | each emitting module |

Rules: capability checks, not hard deps (`session.spawn` absent → job stays
queued, no error); hooks never call providers; memory never imports survey/CI
(constellation rule); `context-sleep` is retired from this bundle (D11).

## 4. Phases and tasks

Each task = one or a few small commits + tests. Acceptance is machine-checked.

### P0 — measure and hygiene
- **T0.1** Bump amplifier-data pin 09482f1→3ef751b (docs-only delta). *Accept:* tool-memory suite green.
- **T0.2** Drawer time/provenance facts `filed_at`, `in_session`, `at_commit` in `store.py file()`; `garden` `lookback_days` becomes real. *Accept:* unit test asserts facts present and garden filters by age.
- **T0.3** Secret scrub before capture (`hooks-memory-capture`): pattern set (provider keys `sk-…`, `sk-ant-…`, GitHub `gh[pousr]_…`, AWS `AKIA…`, JWT, PEM private-key blocks, `KEY=`/`TOKEN=`/`SECRET=`/`PASSWORD=` env assignments), replacement `[REDACTED:<kind>]`, event `memory:capture_redacted` (counts only). *Accept:* test files a fake key and proves it is absent from the store.
- **T0.4** AMB adapter `benchmarks/amb/` + runner that registers our provider at runtime (no AMB code copied); baseline recorded in `EXPERIMENT_JOURNAL.md` (LongMemEval-S, PrecisionMemBench: R@5, R@10, QA acc, context tokens, p95 retrieve ms). *Accept:* runner smoke test on a 5-doc fixture offline (mocked AMB types); real run is an experiment entry.

### P1 — retrieval
- **T1.1** amplifier-data `lenses/bm25.py` (pure fold, scope filter-before-score, deterministic tie-break). *Accept:* upstream tests: exact-identifier query ranks the identifier doc #1; scope respected.
- **T1.2** `store.search` RRF fusion across semantic+BM25 on the shared `_SearchFold`; per-hit `arms`. *Accept:* a doc containing `ERR_4417` with no semantic neighbour is recovered (fails on v2.0.1 path); v2.0.1 equivalence tests updated deliberately.
- **T1.3** Temporal filter `since`/`until` on search. *Accept:* range test.
- **T1.4** Latency gate test: p95 search on a 5k-drawer synthetic store under budget recorded in config.

### P2 — facts and reflection
- **T2.1** Fact model + tool ops `fact_add`, `fact_supersede`, `facts` (query, `current_only`), provenance required (op rejects a fact with no existing source drawer). *Accept:* tests for provenance rejection, supersede chain, proof_count.
- **T2.2** Search returns facts first-class (`layer:"fact"`, citing drawers).
- **T2.3** `context/reflection-rubric.md` (own words; Letta/Mem0/Hindsight-derived rules: corrections first, noop allowed, absolute dates vs observation date, 15–80 words, don't re-record the searchable, preserve history).
- **T2.4** `agents/distiller.md` (`model_role: [fast, general]`, tools: memory only).
- **T2.5** `hooks-memory-reflect`: subscribe `context:compaction`, `context:pre_compact`, `session:end`; watermark over `coordinator.get("context").get_messages()`; durable `reflection_job` cell; spawn distiller via `session.spawn` in a task joined by `register_cleanup`; per-session job cap; events `memory:reflection_queued|started|completed|skipped`. *Accept:* tests with fake context + fake spawn prove: span = messages since watermark, no double-reflection, no-op without spawn capability, job persisted.
- **T2.6** Handle `memory:curator_handoff_requested` for real (spawn curator the same way) or remove the dead event. *Accept:* test.
- **T2.7** Single-valued predicate rule + integrity tension for unresolved contradictions.

### P3 — index and briefing
- **T3.1** Tool ops `index_build(scope)`, `index` (read); curator rollup bottom-up; freshness counts.
- **T3.2** Briefing = L3 index lines + standing answers + top current facts, within existing token budget; verbatim drawers only as cited snippets.
- **T3.3** Standing questions + retraction check.

### P4 — feedback
- **T4.1** Register and emit `memory:retrieved` (query, refs, arms, layer) and `memory:cited` (refs present in the final context). Stable ids = content refs (resolves backlog "stable memory ids").
- **T4.2** Conductor (behavioral-plasticity repo) consumes them; memory stays event-only.

### P5 — cleanup
- **T5.1** Remove `modules/context-sleep` (study doc kept) after confirming no consumer.

## 5. Out of scope (recorded, not forgotten)
ANN index (until p95 gate fails), MMR, cross-encoder, graph expansion arm, skill learning, per-client ACL, stored code graph.

## 6. Measurement bar ("best of all the tools") — D13, proposed
1. AMB LongMemEval-S and LoCoMo QA accuracy ≥ our v2.0.1 baseline + 5 pts and ≥ AMB hybrid-search baseline (74.0 LME) under identical AMB answer/judge models.
2. PrecisionMemBench R@10 ≥ v2.0.1.
3. Context tokens per answer ≤ 50% of v2.0.1 briefing at equal or better accuracy.
4. Hot path: interject/search p95 ≤ 300 ms on 5k drawers; zero LLM calls on hot path (asserted by test).
Head-to-head against Hindsight/Mem0 is run through AMB's own providers when keys allow; otherwise their self-reported numbers are quoted as self-reported.
