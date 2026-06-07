# Handoff

*Last updated: 2026-06-05 — autonomous weekend build (Phase 1 + Phase 2)*

## TL;DR for Michael (Sunday)

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
4. **Substrate:** Phase 2 writes through `PalaceMemoryStore` (ChromaDB palace). amplifier-data is a declared seam only (stub), **blocked** on persistence + vector lens.

## Blocked / Unresolved (need your input)

- **Substrate fork (the big one):** commit to amplifier-data as the FILE target → fund persistence + vector lens in the amplifier-data repo? Or stay on the palace and keep amplifier-data concept-only? Phase 3 is gated on this.
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
