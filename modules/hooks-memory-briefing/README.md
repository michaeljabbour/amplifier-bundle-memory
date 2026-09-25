# amplifier-module-hooks-memory-briefing

Amplifier hook module — injects a memory wake-up briefing at session start.

## Delivery and latency (2.1.0)

amplifier-core discards the result of `session:start` hooks, so the briefing
is **prefetched in the background from `mount()`** and **injected at the first
`prompt:submit`** (with `ephemeral=True`; see below for what the default
orchestrator does with that). `session:start` only arms delivery and never
blocks on memory I/O.

| Config | Default | Meaning |
| --- | --- | --- |
| `deliver_wait_s` | `2.0` | Longest the first `prompt:submit` waits for a still-running prefetch. Later delivery points (`provider:request`, next `prompt:submit`) never wait. |
| `cache_ttl_s` | `300` | Per-process reuse of the prefetched memory sections (coordination files are re-read at delivery). |
| `brief_subsessions` | `false` | Sub-agent sessions (`session:start` with a `parent_id`) get no briefing. |

### Behavior changes and cost (read before upgrading)

- **Tokens.** Before 2.1.0 nothing was delivered (the `session:start` result
  is discarded). Now the briefing reaches the model: roughly 300-1,500 tokens
  (`token_budget`) added on turn one.
- **Not turn-one-only under the default orchestrator.** The injection is
  `ephemeral=True`, but `loop-streaming`'s default
  `ephemeral_injection_mode: persist` stores it as a user message that
  `context-simple` protects from compaction, so it stays in the context for
  every later request of the session (cached, but counted against the window).
- **Sub-agent sessions** get no briefing unless `brief_subsessions: true`.
- **Staleness.** Memory sections are reused per process for `cache_ttl_s`
  (300 s): a second session in the same process within that window sees the
  first session's memory sections, not writes made in between. Results where
  any lookup failed are never reused.
- **Exit.** The prefetch runs on daemon threads; a session that ends before it
  finishes abandons it instead of holding the process open.

`MemoryBriefingHook.__call__` remains the synchronous build-and-return entry
point for hosts that honor an injection wherever they call it.

Measure with `scripts/bench_startup.py` (private daemon on a copy of a store
log; never touches `~/.amplifier/memory` or the shared tool environment).
