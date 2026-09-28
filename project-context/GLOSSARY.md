# Glossary

<!-- Format: Term | Means | Does NOT Mean -->

| Term | Means | Does NOT Mean |
|------|-------|---------------|
| Drawer | L1 evidence: one verbatim, content-addressed memory cell | A summary |
| Fact | L2: a 15–80-word distilled statement with ≥1 `@memory:derived_from` edge to drawers | Ground truth; anything without provenance |
| Index / abstract | L3: per-room/wing navigation text (≤256-char abstract, ≤4000-char overview) | Evidence; never cited |
| Standing question | A question whose cached answer cites facts; stale when a cited fact is superseded | A saved search |
| Supersede | New fact + `@memory:supersedes` edge + old fact loses `@memory:current` | Delete or overwrite |
| Watermark | Index into the context's full message history up to which reflection already ran | An evicted-message list |
| Reflection | Cold-path distillation of a conversation span into facts by the distiller agent | Context compaction |
| Hot path | Per-tool-call / per-prompt code; no LLM calls, bounded latency | Session-end work |
| Arm | One retrieval method (semantic, BM25, temporal) whose ranks are fused by RRF | A reranker |
| Gene transfer | Reimplementing a mechanism from another project in our shape | Vendoring or copying code/prompts |
