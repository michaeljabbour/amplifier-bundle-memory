# Memory-systems gene survey (2026-09-27)

Evidence record behind decisions D6–D15 in `project-context/PROVENANCE.md` and the
design in `docs/plans/2026-09-27-memory-layers-design.md`. Mechanism-level reads
of source (not READMEs alone) by five parallel research lanes, plus an internal
baseline survey. **Nothing here is vendored** (D6): mechanisms are reimplemented
in Amplifier shape; licenses are listed so that stays true.

Source trigger: @Lummox_eth, "10 open-source GitHub projects that stop agents
from starting from zero" (x.com/lummox_eth/status/2104218875810185287).

## 0. Cross-cutting findings

| # | Finding | Evidence |
|---|---|---|
| F1 | Every mature system converged on **three layers**: evidence → distilled facts (with provenance) → navigation index. We have only evidence. | Mem0 facts; Hindsight world/experience/observation + mental models; Letta core/recall/archival; OpenViking L0/L1/L2; memU RecallFile/segments |
| F2 | The field is moving **toward append-only**: Mem0 v2 removed its own UPDATE/DELETE; Graphiti/Mem0g soft-invalidate. Our substrate is already append-only. | `mem0 docs/changelog/sdk.mdx` ("no more UPDATE/DELETE events"); Graphiti `edge_operations.py` `resolve_edge_contradictions` |
| F3 | Our 96.6% R@5 is **retrieval-only** and partly measured on a synthetic proxy; peers report **end-to-end QA accuracy** under an LLM judge. Not comparable. | README eval table; Hindsight paper arXiv 2512.12818 (no R@k anywhere) |
| F4 | Plain files + grep scored 74.0% LoCoMo (Letta), above Mem0g's reported 68.5% — structured memory must earn its place against grep. | letta.com/blog/benchmarking-ai-agent-memory |
| F5 | Hybrid retrieval (semantic + BM25, rank-fused) is in Mem0, Hindsight, Graphiti. Our lexical term only reranks vector top-3k, so it can never recover a vector miss. | `store.py:700-825`; Hindsight `search/fusion.py` (RRF k=60); Graphiti `search_config.py` |
| F6 | Mid-session learning is triggered on **compaction** (Letta Code "dreaming", default trigger `compaction-event`). | letta-code `src/cli/helpers/memory-reminder.ts:52` |
| F7 | Vendor benchmark numbers are contested (Mem0 vs Zep LoCoMo dispute; Hindsight's AMB is built by its own vendor). Report retrieval recall AND QA accuracy, reproducibly, locally. | blog.getzep.com "lies-damn-lies"; github.com/getzep/zep-papers/issues/5 |

## 1. Per-project mechanisms (what we take)

### Mem0 (Apache-2.0) — github.com/mem0ai/mem0
- v2 write path: ONE extraction call, `ADDITIVE_EXTRACTION_PROMPT` ("Your sole operation is ADD"), facts self-contained 15–80 words, `attributed_to`, `linked_memory_ids`; relative dates grounded to **Observation Date**, not current date. (`mem0/memory/main.py` `_add_to_vector_store`)
- UUID→small-integer remap inside prompts to stop ID hallucination.
- MD5 exact dedup; LLM-free entity extraction (`utils/entity_extraction.py`), entity match at cosine ≥0.95.
- Scoring: semantic gate, then (semantic + sigmoid-normalised BM25 + damped entity boost)/max (`utils/scoring.py`).
- Paper (arXiv 2504.19413) LOCOMO: Mem0 66.88 / Mem0g 68.44 / full-context 72.90; 7k vs 26k tokens. Vendor-reported.
- **Genes:** G-facts-with-provenance, G-observation-date, G-int-id-remap, G-content-hash-dedup.

### OpenMemory (v1 MCP server inside mem0 @ v1.0.9; `mem0ai/openmemory` is now an MIT session-porting tool)
- Per-client "App" scoping, allow/deny ACL, pause, state history, `MemoryAccessLog` on every read (`openmemory/api/app/models.py`, `utils/permissions.py`).
- **Gene:** G-access-log (read events = usage signal). ACL deferred.

### Hindsight (MIT) — github.com/vectorize-io/hindsight
- Types now `world | experience | observation` (opinion removed, `retain/types.py:331`); three times per fact: `occurred_start/end`, `mentioned_at`.
- Recall: 4 parallel arms (vector, BM25, graph expansion from top-20 with per-entity fanout cap 200 and common-entity damping, temporal range via rule parser), RRF k=60 with per-arm caps, cross-encoder rerank, token-budget packing (`search/retrieval.py`, `fusion.py`, `link_expansion_retrieval.py`).
- Consolidation into observations with `proof_count`, rules "prefer update over create / no computation / preserve history" (`consolidation/prompts.py`, `consolidator.py:414`).
- Mental models = standing questions with cached answers; `reflect/retractions.py` flags models citing invalidated facts.
- **Genes:** G-rrf, G-temporal-arm, G-proof-count, G-standing-questions, G-retraction-check, G-graph-expansion (later).

### Agent Memory Benchmark (NO LICENSE FILE) — github.com/vectorize-io/agent-memory-benchmark
- Adapter = subclass `MemoryProvider` (`memory/base.py`): `ingest(list[Document])`, `retrieve(query,k,user_id,query_timestamp)`; register in `REGISTRY`. Datasets carry `gold_ids` → R@k and QA side by side. Defaults: answer model Groq gpt-oss-120b, judge Gemini.
- **Use:** run as an external dev tool; never copy its code (no license).

### Graphiti (Apache-2.0) — github.com/getzep/graphiti ; Zep paper arXiv 2501.13956 (CC BY-NC-SA — paraphrase only)
- Bi-temporal edges: `valid_at/invalid_at` (world) + `created_at/expired_at` (system).
- Contradiction: one scoped LLM call over same-entity-pair duplicates + invalidation candidates; then deterministic `resolve_edge_contradictions` (non-overlapping windows skipped; older edge gets `invalid_at=new.valid_at`, `expired_at=now`; nothing deleted).
- Entity dedup cascade: exact normalized → entropy-gated MinHash/Jaccard ≥0.9 → LLM (`dedup_helpers.py`).
- Search: cosine + BM25 + BFS; rerankers RRF, MMR λ=0.5, node-distance, mentions, cross-encoder.
- **Genes:** G-bitemporal, G-supersede, G-cheap-first-dedup, G-mmr (later).

### Cognee (Apache-2.0) — github.com/topoteretes/cognee
- `functional_relationships`: single-valued predicates → newest wins, older superseded, no LLM.
- Code graph delegated to external `enola`; stable ids `sha256(repo,kind,name,file)`.
- memify `apply_feedback_weights` (usage-weighted graph elements).
- **Genes:** G-single-valued-predicates, G-code-anchor (SHA + stable symbol id; NOT a stored code graph), G-feedback-weights (via conductor).

### Letta / Letta Code (Apache-2.0) — github.com/letta-ai/letta-code
- MemFS: git-backed markdown memory; root `MEMORY.md` index; always-loaded core vs deferred children; `ARCHIVE.md`.
- Reflection ("dreaming") triggered on compaction or step-count, in a worktree.
- Reflection rubric (`reflection-v2.md`): corrections first → preferences → facts → contradictions → procedures; drop ephemeral / already-captured / non-generalizable; absolute dates; fix contradictions at source; "the raw conversation is already searchable — don't re-record it"; ≤1 skill op per pass.
- `/remember` routes to the right tier; `memory_replace` requires a unique match.
- Skill learning: +15.7 pts Terminal-Bench 2.0 with verifier feedback (vendor-reported).
- **Genes:** G-compaction-trigger, G-reflection-rubric, G-index-file, G-remember-routing, G-skill-learning (later).

### memU (Apache-2.0) — github.com/NevaMind-AI/memU
- Agent-as-author: read file *names* first, open bodies only if related, choose no-op / patch / create; name+description front-matter; one segment per line, file scored by best segment (`hosts/bridging/instructions.py`, `app/agentic.py`).
- 92.09% LoCoMo is from a prior architecture (memu.pro/benchmark disclaimer).
- **Genes:** G-noop-allowed, G-best-segment-rollup.

### OpenViking (AGPLv3 core — ideas only, never code) — github.com/volcengine/OpenViking
- Per-directory L0 `.abstract.md` (≤256 chars, embedded) and L1 `.overview.md` (≤4000 chars), bottom-up rollup from children's L0, freshness front-matter.
- Hierarchical retrieval: global top-10 picks directories, then priority-queue descent; stop after 3 unchanged rounds.
- Hotness = sigmoid(log1p(access)) × exp-decay (7-day half-life), default weight 0.
- Commit-time skip/create/merge/delete + `memory_diff.json` audit.
- **Genes:** G-room-abstracts, G-two-stage-scoped-search, G-memory-diff, G-hotness (low weight).

## 2. Genes explicitly rejected (and why)

| Gene | Source | Why rejected |
|---|---|---|
| Stored code graph | Cognee/enola | Derivable from repo, stale per commit, duplicates LSP/grep; take stable anchors instead |
| Community detection | Graphiti | Needs large corpus + hosted graph DB; no evidence of value at our scale |
| Cross-encoder on hot path | Hindsight/Graphiti | Violates v2.0.1 latency lesson; allowed only on cold/opt-in paths |
| Disposition traits | Hindsight | Persona policy, not memory mechanism |
| Destructive UPDATE/DELETE | Mem0 v1 | Superseded by Mem0 itself; conflicts with append-only substrate |
| Separate graph DB (Neo4j/Kuzu) | Mem0g/Graphiti/Cognee | amplifier-data already provides facts/edges/lenses |
| Agent File (.af) | Letta | Deprecated upstream |

## 3. Internal baseline (v2.0.1, 2026-09-27)

- Evidence layer: content-addressed drawers, wing/room/agent scopes, category/importance as facts. **No wall-clock time on drawers** (`garden.py:31-35` documents `lookback_days` as a no-op) — amplifier-data deliberately excludes wall-clock from events (`lenses/temporal.py:13-18`), so time must be consumer-supplied facts.
- Retrieval: MiniLM-L6 384-d, brute-force cosine; `0.85·cos + 0.15·lexical` over vector top-3k; importance rerank in briefing.
- **Curator is never auto-invoked**: `hooks-project-context` emits `memory:curator_handoff_requested` at `session:end`; nothing handles it.
- `context-sleep` (a context manager) is not composed by any behavior; it blocks the turn on an LLM call and emits a non-registered `context:sleep_complete`.
- No secret redaction on capture.
- amplifier-data 09482f1→3ef751b: docs/metadata only (safe bump). No lexical lens; lenses are pure-Python folds (copy `lenses/vector.py`); `integrity.py` tensions can model contradictions; `intent.py` shows the derived-cell + provenance-edge pattern.

## 4. Amplifier-shape facts (from core/foundation experts, verified)

- Compaction is context-manager policy inside `get_messages_for_request()`; `get_messages()` always returns full history (`amplifier-core CONTEXT_CONTRACT.md`).
- Standard foundation `context-simple` emits **`context:compaction`** with stats only (`before/after_tokens`, `messages_removed`, …) — no evicted list (verified in installed module `__init__.py:1481-1501`). Kernel also defines `context:pre_compact`/`context:post_compact` (`events.rs:105-110`).
- Hooks run in-loop; background work = `asyncio.create_task` joined via `coordinator.register_cleanup`; `session:end` fires before cleanups (`session.py:240-257`).
- Background LLM work belongs in a spawned agent session via the app-layer `session.spawn` capability (used by tool-recipes, dot-runner), with `model_role` declared in agent frontmatter — never a hook calling a provider directly.
- Custom events must be registered via the `observability.events` contribution channel.
