# context-sleep — Research Findings & Integration Notes

This documents the empirical study behind the `modules/context-sleep/` Context
module: a port of *Language Models Need Sleep* (arXiv:2605.26099) — "when context
fills, run an offline consolidation pass" — to Amplifier agent context management.

**Bottom line:** the module is a correct, opt-in reference implementation. The
research does **not** support promoting it to a default or marketing it as a
reasoning improver. Its honest value is lossless compression of *redundant/verbose*
context plus a future memory-store bridge — not "metabolizing context into a
reasoning-ready state."

---

## What was tested

Two complementary studies (full harness + history live in the `amplifier-research-sleep`
research project; distilled here):

1. **Set-coverage proxy** — controlled benchmark of 6+ context strategies under a
   fixed token budget.
2. **Real mechanism** — actual LLM consolidation (N recurrent passes,
   query-agnostic) on real multi-hop relational reasoning. Wake model `gpt-5`
   (verified oracle accuracy = 1.00 at every depth — capability precondition holds);
   consolidator `gpt-4.1` (deliberately weaker-at-reasoning, so any gain can't be
   "a genius reasoned offline"); deterministic gold grading; paired McNemar +
   seed-cluster bootstrap.

## Findings (adversary-corrected, n=192 paired)

| Question | Result |
|---|---|
| Does faithful LLM consolidation beat dumb verbatim retention (reasoning)? | **No — equivalent** once representation format is controlled (delta ~= 0, p=0.69-1.0) |
| Does "creative" reorganize/derive consolidation help? | **No — it HURTS** (-28pp; fabricates ~50-75% of facts) |
| Does "more sleep" (more passes) help? (paper's signature claim) | **No effect** (CI [-5.7, +4.7]) |
| What actually moves the needle? | **Filler-stripping** (+19pp vs recency truncation, no LLM) and **representation format** (natural-language sentences beat terse triples by ~19pp, p<1e-6) |

The first headline ("dumb retention beats LLM consolidation") was itself a **format
confound** caught by adversarial review — verbatim used sentences, the LLM used
triples. Controlling format, the consolidation effect vanished. Two real code bugs
(a dead distractor parameter; a first-vs-last answer-extraction bug) were also
caught and fixed before the corrected result. Without the adversary, a wrong causal
claim would have shipped — that correction is the result.

## Why these guardrails are in the module

The module encodes the findings as hard defaults (see `modules/context-sleep/README.md`):

- **Faithful-by-default; `creative` is gated behind a loud warning** — creative
  consolidation fabricates facts by design.
- **Natural-language format preserved** — representation alone swings accuracy ~19pp.
- **Single pass** — additional passes gave no measurable benefit.
- **Non-LLM verbatim fallback** — a provider hiccup never loses data.

## Integration recommendation

- **Keep opt-in.** Committed under `modules/context-sleep/` (27/27 tests), **not**
  wired into `behaviors/memory.yaml`. Leave it that way until there's a workload
  it demonstrably helps. Replacing `context-simple` expecting better reasoning is
  not supported by evidence.
- **Correct use case:** compressing repetitive/verbose context (e.g. long tool-output
  transcripts) where there is genuine redundancy to remove — not already-atomic facts.
- **The genuinely complementary win (future, 1-file):** a hook on the module's
  `context:sleep_complete` event that writes consolidated facts to memory —
  coupling working-memory compaction (the module) to long-term semantic memory
  (this bundle's existing strength).

## Prior art (don't duplicate)

`amplifier-module-context-simple` (truncation/ephemeral compaction, no LLM) and
`amplifier-bundle-context-managed` (LLM rolling summarization) already occupy the
LLM-summarization niche. `context-sleep` is distinguished only by faithful
two-buffer consolidation + the memory-store bridge — both memory-bundle concerns,
which is why it lives here rather than as a standalone bundle.
