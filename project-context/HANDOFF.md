# Handoff

*Last updated: 2026-07-07 — substrate-adapter completion*

## TL;DR for Michael (2026-07-07 session)

Implemented `docs/plans/2026-07-07-substrate-adapter-completion-design.md` in
full: the `AmplifierDataMemoryStore` seam now routes vectors, KG facts, and
diary entries through amplifier-data (not just drawers), `file()`/
`file_diary()`/`update_importance()` are atomic on capable backends (the
stale `update_fact`/`append_batch` probe is replaced with the real
`write_batch` primitive), the gateway gained `add_embedding`/`query_vector`/
`batch` parity, `tool-mempalace` gained a best-effort shadow for `kg` and
`diary` ops, and `dualwrite_compare.py` now verifies vector/KG/scope/diary
read-consistency in addition to E1/scope/facts/durability. **Not committed**
— left on the working tree for review.

**Killer gates KG-V1…KG-G1:** all encoded as tests and green in the
substrate-installed dev venv (`~/dev/.venv`, amplifier-data editable at HEAD
`09482f1`). See "Verification evidence" below for exact counts.

**Pin bump (flag for the conductor):** `modules/tool-mempalace/pyproject.toml`
`[substrate]` extra bumped `c1107b4` → `09482f1fa569ba8407894cad3a32f8ab6aecbc3d`
(Plan 4a intent-compiler + Plan 4b convergent-integrity now included). **The
matching pin in `amplifier-bundle-behavioral-plasticity`'s
`modules/dep-amplifier-data/pyproject.toml` `[project.dependencies]` MUST move
to the SAME SHA in lockstep** — that repo is out of scope here (owned by the
conductor bundle) and was NOT touched.

## TL;DR for Michael (Sunday, 2026-06-05 — prior session)

Built and committed **Phase 1 (manifest)** and **Phase 2 (curate.dot pipeline)** of
the memory architecture, on branch `feat/manifest-and-curate-pipeline`. Everything
is TDD'd and verified green. **Not pushed, no PR opened** — left for your review.

```
Branch:   feat/manifest-and-curate-pipeline  (off main)
Commits:  1297ce6  feat(memory): externalize capture taxonomy into a user-editable manifest   (Phase 1)
          dc978c8  feat(memory): add opt-in attractor curate.dot consolidation pipeline        (Phase 2)
Pushed:   NO        PR: NONE     (your call)
```

## Accomplished

### Phase 1 — the "knowable list" (Capture Manifest)
- `context/memory-manifest.yaml` — the editable list of what memory captures (7 attractors: seeds + importance_base + emergent policy).
- `modules/tool-mempalace/.../manifest.py` — pure loader. `load_manifest()` resolution order: explicit `manifest_path` → `<project>/project-context/memory-manifest.yaml` → `~/.amplifier/memory-manifest.yaml` → in-code `DEFAULT_MANIFEST`. The default **mirrors the legacy hardcoded behavior exactly**, so no-manifest deployments behave identically. Graceful fallback on missing/malformed files. **23 tests.**
- `hooks-mempalace-capture/__init__.py` — now loads category signals from the manifest (zero-LLM, hot-path safe). New `manifest_path` config knob. `_detect_category(text, signals=None)` keeps legacy default. **5 tests.**
- `phase3.py` — added `base_overrides` param to `compute_importance` / `plan_phase3_actions` so the manifest's `importance_base` can flow into the rubric. Default `None` = legacy behavior unchanged. **9 tests.**
- `behaviors/mempalace.yaml` — documented the `manifest_path` knob.

### Phase 2 — the user-steerable cold-path pipeline (curate.dot)
- `pipelines/curate.dot` — consolidation pipeline: `load → dedup → classify → verify[goal_gate convergence] → write`. **VERIFIED: parses (7 nodes) + passes `validate_or_raise` against the REAL amplifier-bundle-attractor engine.**
- `scripts/memory_store.py` — the storage seam: `MemoryStore` protocol, `RecordingMemoryStore` (tests/dry-run), `PalaceMemoryStore` (real, shells `mempalace add_drawer`), `AmplifierDataMemoryStore` (**deliberate loud `NotImplementedError` stub** for the Phase 3 substrate).
- `scripts/load_captures.py` + `scripts/write_cells.py` — the two pipeline node scripts, registered as `[project.scripts]` entry points (`mempalace-load-captures`, `mempalace-write-cells`) so curate.dot tool nodes resolve on PATH. **12 tests** (9 scripts + 3 curate.dot).
- `behaviors/curate.yaml` — **OPT-IN** behavior pulling attractor's `run_pipeline` tool + Curator. Deliberately NOT included by `behaviors/mempalace.yaml` (no hard dependency on attractor).
- `agents/curator.md` — documented the on-demand "consolidate my memory" flow with graceful optionality.

## Verification evidence (run these to reproduce)

Test runner is the shared dev venv: `../.venv/bin/python` (3.12.4, has yaml 6.0.3 + pytest 9.0.2).
Run per-module (a combined invocation hits a pytest duplicate-`tests`-package collection error — a known invocation artifact, not a code defect):

```
cd ~/dev/amplifier-bundle-memory
../.venv/bin/python -m pytest modules/tool-mempalace/tests -q
    → 149 passed, 2 skipped         (includes manifest 23, phase3-overrides 9, scripts 9, curate.dot 3)
../.venv/bin/python -m pytest modules/hooks-mempalace-capture/tests -q
    → 5 passed
../.venv/bin/python -m pytest modules/hooks-mempalace-briefing/tests -q
    → 12 passed
```

### 2026-07-07 substrate-adapter-completion evidence

Test runner: `~/dev/.venv/bin/python` (3.12.4), with amplifier-data **editable-installed**
from `~/dev/amplifier-data` at HEAD `09482f1` (`RUST_AVAILABLE=True` — durable-store
gates run against the real Rust kernel, not just the in-memory fallback).

```
cd ~/dev/amplifier-bundle-memory
~/dev/.venv/bin/python -m pytest modules/tool-mempalace/tests -q -rs
    → 190 passed, 2 skipped   (skips are PRE-EXISTING: "mempalace CLI not available",
                                unrelated to substrate work; ALL substrate-gated tests
                                EXECUTED, not skipped, since amplifier_data is installed)
~/dev/.venv/bin/python -m pytest modules/hooks-mempalace-capture/tests -q
    → 8 passed                (was 5; +3 net from this session's protocol-kwarg
                                regression coverage + pre-existing shadow tests)
~/dev/.venv/bin/python -m pytest modules/hooks-mempalace-briefing/tests -q
    → 12 passed               (unchanged, regression-green)
~/dev/.venv/bin/python -m pytest tests -q --ignore=tests/integration
    → 111 passed, 2 deselected  (repo-root contract tests; tests/integration/ is
                                   DTU-gated and excluded here by design)
```

Killer gates KG-V1…KG-G1, all EXECUTED (not skipped) and green:

| Gate | Test(s) |
| --- | --- |
| KG-V1, KG-V2 | `test_amplifier_data_store.py::test_vector_top1_self_retrieval_and_scope_isolation`, `::test_embedding_regenerates_byte_identical_after_reopen` |
| KG-A1 | `::test_update_importance_atomic_success_and_crash_injection`, `::test_update_importance_non_atomic_on_remote_store` |
| KG-A2 | `::test_file_atomic_single_append_batch_with_embedding` |
| KG-K1 | `::test_assert_kg_query_and_timeline` (seam half) + `test_tool_shadow_ops.py` (tool-wiring half, all 3 tests) |
| KG-K2 | `test_amplifier_data_store.py::test_kg_phase3_shaped_facts_traverse_and_resolve` |
| KG-D1 | `::test_file_diary_scoped_and_sourced` |
| KG-R1 | `test_dualwrite_compare.py` (3 tests) + manual CLI run below |
| KG-P1 | `test_substrate_pin.py::test_substrate_extra_resolves_pinned_sha` (fresh-venv `uv pip install` of the exact pinned SHA; verified `vcs_info.commit_id` match + `callable(AmplifierStore().write_batch)`) + `::test_write_batch_callable_in_this_process` |
| KG-G1 | `test_amplifier_data_gateway.py::TestGatewayVectorAndBatchParity` (4 tests: round-trip, scoped query, auth-required, atomic batch) |

`mempalace-dualwrite-compare` CLI, run end-to-end against a temp store (representative
corpus, synthetic vectors — no local palace/ChromaDB export available in this environment):

```
cd ~/dev/amplifier-bundle-memory
PYTHONPATH=modules/tool-mempalace ~/dev/.venv/bin/python -m \
  amplifier_module_tool_mempalace.scripts.dualwrite_compare --events-dir /tmp/nonexistent
    → PASS, exit 0. All fields green: e1_byte_identical=8/8, scope_edges_ok=8/8,
      durable_reopen_ok=8/8, durable_vector_ok=8/8, facts_ok=7/7,
      vector_top1_ok=8/8=vector_scoped_total, kg_assert_ok/kg_invalidate_ok/
      kg_timeline_ok/scope_query_consistent/diary_ok = true.
```

`ruff check` on every new/changed file: clean (0 issues after removing one unused
import). `pyright` (module's own `[tool.pyright]` config, run from
`modules/tool-mempalace/`) on every changed `.py` file: **0 errors, 0 warnings**.
The `python_check` aggregate tool separately reports pre-existing,
unrelated-to-this-work findings across the wider package (ruff-format drift in
files this session did not touch — `garden.py`, `manifest.py`, `phase3.py`,
`load_captures.py`, `server_concurrency_check.py`; and `reportMissingImports`
on intra-package imports across ALL `scripts/*.py` files, including ones this
session did not touch — an artifact of that tool not resolving the editable
package path, not a real import failure, confirmed by the clean manual
`pyright` run above). One pre-existing 108-char line
(`amplifier_module_tool_mempalace/__init__.py`, the `mine` operation's
`mode` schema description) shifted position due to this session's additions
earlier in the file; its content is git-identical to HEAD and the repo's own
ruff config does not select `E501`, so this is not a regression.

curate.dot validated against the real attractor engine:
```
../.venv/bin/python -c "import sys; sys.path.insert(0,'$HOME/dev/amplifier-bundle-attractor/modules/loop-pipeline'); \
from amplifier_module_loop_pipeline.dot_parser import parse_dot; \
from amplifier_module_loop_pipeline.validation import validate_or_raise; \
g=parse_dot(open('pipelines/curate.dot').read()); validate_or_raise(g); print('curate.dot OK', len(g.nodes), 'nodes')"
    → curate.dot OK 7 nodes
```

`python_check`: **0 errors.** Warnings: 1 intentional STUB (`AmplifierDataMemoryStore` raises `NotImplementedError` by design — a test asserts it). 3 pre-existing pyright `SyncBridge` typing warnings in the capture hook are unrelated — confirmed present at HEAD before this work.

## Decisions I locked (autonomous defaults — override if you disagree)

See `PROVENANCE.md` for full rationale. Summary:
1. **Manifest scope:** per-project preferred, with `~/.amplifier/` then in-code default fallback.
2. **Cold-path trigger:** on-demand only for Phase 2 (Option C, per foundation-expert). Volume-gated `session:end` hook deferred to a follow-on, and must use a Python capability check (never a hard YAML dep).
3. **Emergent policy:** declared in the manifest schema (`emergent.enabled`, default **false**); pipeline can propose, user confirms. Not yet wired into classification logic.
4. **Substrate:** ~~Phase 2 writes through `PalaceMemoryStore` (ChromaDB palace). amplifier-data is a declared seam only (stub), **blocked** on persistence + vector lens.~~ **RESOLVED 2026-07-07 (see D5 in PROVENANCE.md):** the fork resolved toward composition — amplifier-data's persistence + vector lens landed upstream, and the seam is a completed consumer adapter (vectors, KG facts via anchor cells, diary entries, atomic `write_batch`). The palace remains the production read source; the substrate is a shadow/verify target. Read-path cutover is a separate, still-open policy decision (see "Start Here Next Session" below).

## Blocked / Unresolved (need your input)

- ~~**Substrate fork (the big one):** commit to amplifier-data as the FILE target → fund persistence + vector lens in the amplifier-data repo? Or stay on the palace and keep amplifier-data concept-only? Phase 3 is gated on this.~~ **RESOLVED 2026-07-07** — see D5 in PROVENANCE.md and point 4 above.
- **NEW (2026-07-07): read-path cutover policy.** The substrate can now answer memory's read shapes (vector, KG, scope, diary) per `dualwrite_compare.py`'s new checks, but the palace is still the ONLY production read source. Deciding when/whether to cut reads over to amplifier-data (partially or fully) is an open policy call — out of scope for the substrate-adapter-completion design by intent (§9 "Explicitly out of scope").
- **NEW (2026-07-07): ChromaDB-behind-`VectorBackend`.** COMPOSITION.md's target state is ChromaDB as the ANN accelerator *behind* the substrate's `VectorBackend` stud (rebuilt from `iter_embeddings()`), not vectors living in two independent places. This change only makes the substrate the *write* target for vectors; the accelerator wiring is a follow-on.
- **NEW (2026-07-07): conductor pin lockstep.** The `[substrate]` extra pin bump (`c1107b4` → `09482f1fa569ba8407894cad3a32f8ab6aecbc3d`) needs the SAME bump in `amplifier-bundle-behavioral-plasticity`'s `modules/dep-amplifier-data/pyproject.toml`. Not done here (out of scope repo) — flagging so it happens in lockstep, not drifts.
- **End-to-end runtime not exercised.** curate.dot is validated structurally against the real grammar, and every script is unit-tested, but I did **not** run the full pipeline through a live attractor session (needs attractor installed + LLM + the data-threading detail below). I am NOT claiming it runs end-to-end.
- **Known integration gap:** how the `verify` node's `cells` output reaches `mempalace-write-cells` stdin is not yet wired — it needs attractor `context_updates`/`report_outcome` threading. This is the first thing to finish before a live run. Documented, not done.
- **load_captures reads the event log** (`~/.mempalace/events/{sid}.jsonl`), which has previews (~100 chars), not full verbatim drawer content. Full-content consolidation needs a palace query — a deliberate follow-on.

## Start Here Next Session

1. Decide the **substrate fork** and **cold-path trigger** (unblocks Phase 3 + the session:end hook).
2. Wire the `verify → write` data threading (cells → write_cells stdin) and do one live attractor run of curate.dot in a scratch session.
3. If happy: push the branch and open a PR to `main`.
4. Optional follow-on: volume-gated `session:end` consolidation hook (Python capability check), and palace-query-based full-content loading.

## Non-Obvious Context

- **`tests/` (repo root) is DTU-gated** — every test there is skipped outside the memory-bundle-e2e container. Unit tests must live under `modules/*/tests/` (not DTU-gated). I learned this the hard way; the capture-hook tests have their own `conftest.py` that puts the sibling tool-mempalace module on sys.path.
- Modules are **not pip-installed** in the dev venv; pytest's rootdir insertion makes each module's own package importable, cross-module imports need a conftest path hack.
- `AGENTS.md` and `project-context/` are untracked workspace files — I intentionally did **not** commit them in the feature commits.
