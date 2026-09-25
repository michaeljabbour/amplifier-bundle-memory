# amplifier-module-hooks-memory-briefing

Amplifier hook module — injects a memory wake-up briefing at session start.

## Delivery and latency (2.1.0)

amplifier-core discards the result of `session:start` hooks, so the briefing
is **prefetched in the background from `mount()`** and **injected at the first
`prompt:submit`** (ephemeral). `session:start` only arms delivery and never
blocks on memory I/O.

| Config | Default | Meaning |
| --- | --- | --- |
| `deliver_wait_s` | `2.0` | Longest the first `prompt:submit` waits for a still-running prefetch. Later delivery points (`provider:request`, next `prompt:submit`) never wait. |
| `cache_ttl_s` | `300` | Per-process reuse of the prefetched memory sections (coordination files are re-read at delivery). |
| `brief_subsessions` | `false` | Sub-agent sessions (`session:start` with a `parent_id`) get no briefing. |

`MemoryBriefingHook.__call__` remains the synchronous build-and-return entry
point for hosts that honor an injection wherever they call it.

Measure with `scripts/bench_startup.py` (private daemon on a copy of a store
log; never touches `~/.amplifier/memory` or the shared tool environment).
