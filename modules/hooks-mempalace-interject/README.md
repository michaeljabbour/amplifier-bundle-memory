# amplifier-module-hooks-mempalace-interject

Amplifier hook that surfaces relevant memories mid-session at the right moment.

## Read lane

This hook reads the palace exclusively through mempalace's own supported
surface -- the `mempalace_search` tool over the real `mempalace-mcp`
JSON-RPC-over-stdio server. It does **not** open ChromaDB directly and does
**not** hardcode any palace path or collection name, so it structurally
cannot drift from wherever mempalace itself is actually configured to read
and write (verified against the installed mempalace 3.5.0 package:
`~/.mempalace/palace`, collection `mempalace_drawers`).

## Privacy

By default this hook makes **zero external network calls**. Retrieval uses
mempalace's own local embedding model (`all-MiniLM-L6-v2` via ONNX Runtime
by default -- fully offline). The only external call this hook can ever
make is the **optional** LLM judge (OpenAI `gpt-4.1-nano`), used to refine
borderline-relevance matches. It is gated behind `llm_judge_enabled`
(default `false`) -- nothing leaves the machine unless a user explicitly
opts in. When enabled, it sends the current query text and candidate memory
snippets to OpenAI using `OPENAI_API_KEY`.

## Configuration

| Key | Default | Description |
| --- | --- | --- |
| `cosine_threshold` | `0.72` | Minimum similarity to inject |
| `uncertain_band` | `0.10` | Band above threshold that triggers the LLM judge |
| `max_inject_chars` | `800` | Max chars per injection |
| `cooldown_turns` | `3` | Min turns between injections for the same memory |
| `prompt_enabled` | `true` | Enable the `prompt:submit` handler |
| `tool_pre_enabled` | `true` | Enable the `tool:pre` handler |
| `orc_enabled` | `true` | Enable the `orchestrator:complete` handler |
| `llm_judge_enabled` | `false` | **Opt-in.** Sends query + memory text to OpenAI (see Privacy above) |
| `emit_events` | `true` | Emit JSONL events |

