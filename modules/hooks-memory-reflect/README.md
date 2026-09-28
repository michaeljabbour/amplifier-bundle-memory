# amplifier-module-hooks-memory-reflect

Amplifier hook module — cold-path memory reflection (T2.5,
`docs/plans/2026-09-27-memory-layers-design.md` §2/§4).

Subscribes to `context:compaction`, `context:pre_compact`, `session:end` and
`session:start`. On the first three, it watermarks the conversation span
since the last reflection (via the context module's `get_messages()`,
guaranteed full/non-destructive history — see PROVENANCE.md D7), renders it
to plain text, and queues a durable `reflection_job` cell through
`MemoryClient.reflection_job_add()`. If the app layer registers the
`session.spawn` capability (D10), it also spawns the `memory:distiller` agent
in a background task to process the job immediately; otherwise the job stays
pending and is picked up by the `drain_on_start` sweep at the next
`session:start`, or by an on-demand "reflect on pending memory jobs" request.

Never blocks the hot path: `session:end` only ever queues (never spawns, so
session exit is never delayed), and every event handler returns
`HookResult(action="continue")` immediately while the actual work runs in a
tracked `asyncio.Task`. `mount()` registers a `coordinator.register_cleanup`
callback that awaits in-flight tasks up to `drain_timeout_s` before
cancelling — an unfinished task simply leaves its job `pending`, to be
drained next session.

No-ops (never errors) when: running in a child (spawned) session (prevents
reflection-of-reflection recursion), the span is smaller than
`min_new_messages`, or the installed `tool-memory` store predates the
`reflection_job_add`/`reflection_jobs` ops (`store_unsupported`).

See `behaviors/memory-reflect.yaml` for composition (opt-in, requires the
core `behaviors/memory.yaml`).
