# hooks-behavioral-write

T1-HOOK-1: salience-gated, post-session behavioral write hook. Recomputes a
drawer's importance from the session's observed outcome and applies a
reversible, fully-audited update through the amplifier-data seam
(`NativeMemoryStore.update_importance`).

Mounted exclusively by the `amplifier-bundle-behavioral-plasticity` conductor
via a `source:` reference into this subdirectory — it is intentionally not
wired by any `amplifier-bundle-memory` behavior, since the conductor owns the
activation decision.

See the module docstring in `amplifier_module_hooks_behavioral_write/__init__.py`
for the full data flow and mutation contract.
