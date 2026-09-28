# Handoff

*Last updated: 2026-09-28 -- v2.1.0 three-layer memory on `feat/memory-layers` (local, not pushed)*

## TL;DR for Michael (2026-09-28 -- three layers built, gene-transferred, Amplifier-shaped)

Surveyed 10 prior-art memory systems at source level and built the missing
layers in our shape. Nothing vendored. Evidence: external research record:
memthoughts/research/amplifier-memory-v2.1/ (not part of this bundle); design
+ task IDs: `docs/plans/2026-09-27-memory-layers-design.md`; decisions D6-D21
in PROVENANCE.

**Built (all tests green: 629 module/bench + 89 CI-root):**
- L1 evidence: `filed_at`/`in_session`/`at_commit` facts; secret redaction
  before capture; garden `lookback_days` finally real.
- Retrieval: BM25 lens upstream (amplifier-data `feat/bm25-lens`, local) + RRF
  fusion (identifier recall that embeddings miss), `since`/`until`; p95 ~160-190 ms
  on 5k drawers vs 300 ms gate; legacy byte-identical mode kept.
- L2 facts: provenance-mandatory, add-only, supersede-never-delete, single-valued
  predicates, integrity tensions; durable reflection-job queue.
- Reflection: `hooks-memory-reflect` observes standard `context:compaction` /
  `context:pre_compact` / `session:end`, spawns `memory:distiller`
  (`model_role [fast, general]`) via `session.spawn` -- opt-in
  `behaviors/memory-reflect.yaml`.
- L3: mechanism-first index + standing questions (retraction check); layered
  briefing; opt-in `behaviors/memory-index.yaml` curator refresh.
- `memory:retrieved` / `memory:injected` events for the conductor.
- `context-sleep` retired (D11). AMB adapter in `benchmarks/amb/`.

**Needs you (in order):**
1. Push amplifier-data `feat/bm25-lens` (5d7c301, 9b993a1) -> merge -> bump the pin
   in all six module pyprojects (T1.5). Until then production runs legacy
   search (capability check -- safe).
2. Ratify D13's measurement bar, then provide `GEMINI_API_KEY` (+ `GROQ_API_KEY`)
   so the AMB baseline can run (`benchmarks/amb/README.md`). No "better" claim
   until those numbers exist.
3. Push `feat/memory-layers` and validate in a DTU (amplifier-tester) -- the
   spawn path (`session.spawn` -> distiller) is unit-tested with fakes only.
4. T4.2 lives in behavioral-plasticity: consume `memory:injected`.

**Environment note:** `~/dev/.venv` has amplifier-data 0.2.0 from the unpushed
local branch (see WAYSOFWORKING).

**Known limits:** concurrent drains may double-process a job (dedup makes it
harmless); T2.6 curator-handoff event still informational; standing questions
keyed by text across wings.

## Non-Obvious Context

- **`tests/` (repo root) is DTU-gated** -- every test there is skipped outside the memory-bundle-e2e container. Unit tests must live under `modules/*/tests/` (not DTU-gated).
- Modules are **not pip-installed** in the dev venv; pytest's rootdir insertion makes each module's own package importable, cross-module imports need a conftest path hack.
- `AGENTS.md` and `project-context/` are untracked workspace files in some historical branches -- check `git status` before assuming they are committed.

Earlier sessions' handoffs (native cutover, substrate-adapter completion,
Phase 1/2 manifest + curate pipeline) are not reproduced here -- see git
history and `project-context/PROVENANCE.md` for those decisions and their
rationale.
