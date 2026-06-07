# Provenance — Decision Log

## 2026-06-05 — Memory architecture Phase 1 + 2 (autonomous weekend build)

Branch `feat/manifest-and-curate-pipeline`. Decisions made autonomously per
Michael's instruction to "build phase 1 and 2 over the weekend… get it done."
These are defaults; flagged for review.

### D1 — Manifest scope: per-project with fallback chain
**Decision:** `load_manifest` resolves in order: explicit `manifest_path` config →
`<project>/project-context/memory-manifest.yaml` → `~/.amplifier/memory-manifest.yaml`
→ in-code `DEFAULT_MANIFEST`.
**Why:** Per-project is the most useful (different repos accumulate different
knowledge) but must never be mandatory. The in-code default mirrors the legacy
hardcoded table exactly, so existing behavior is preserved with zero config.
**Alternatives:** global-only (rejected: not steerable per project); file-required
(rejected: breaks no-config deployments).

### D2 — Cold-path trigger: on-demand only (Phase 2)
**Decision:** curate.dot runs on-demand via the Curator ("consolidate my memory").
No automatic session:end trigger yet.
**Why:** foundation-expert guidance (2026-06-05): Option C is the right MVP —
zero hot-path risk, works whether attractor is installed or not. A volume-gated
session:end hook is the right follow-on but its optionality must be a Python
capability check (`coordinator.get_tool("run_pipeline") is None → no-op`), never
a hard YAML dependency. Each box node spawns a child LLM session, so automatic
triggering has real cost and must be opt-in + gated.
**Alternatives:** session:end hook now (deferred — cost + optionality complexity);
compose attractor in behaviors/mempalace.yaml (rejected: hard dependency).

### D3 — Emergent policy: declared, default off
**Decision:** `emergent.enabled` (default false) + `promote_threshold` live in the
manifest schema. The pipeline may propose emergent categories; the user confirms.
Not yet wired into classification logic.
**Why:** Keep the precision/recall knob visible and user-owned, but don't ship
auto-promotion behavior unproven. "Propose, don't drop, don't silently park."

### D4 — Substrate: palace now, amplifier-data as a declared seam
**Decision:** Phase 2 writes through `PalaceMemoryStore` (ChromaDB palace).
`AmplifierDataMemoryStore` exists as a loud `NotImplementedError` stub.
**Why:** amplifier-data's Rust floor is proven (E1–E3 green) but it lacks
persistence + a vector lens — the two things memory actually needs. Writing
through a `MemoryStore` seam means swapping to amplifier-data later is a
one-class change, with the gap explicit rather than silent.
**Open fork for Michael:** commit to amplifier-data (fund persistence + vector
lens) vs. stay on the palace and keep amplifier-data concept-only. Phase 3 gated
on this.

### Process note — test placement
Discovered that repo-root `tests/` is DTU-gated (skipped outside the
memory-bundle-e2e container). Unit tests for this work were placed under
`modules/*/tests/` instead, with a local conftest for the capture-hook tests to
put the sibling tool-mempalace module on sys.path. Recorded so the next session
doesn't re-learn it.
