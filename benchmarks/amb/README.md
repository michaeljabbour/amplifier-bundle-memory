# AMB adapter — Agent Memory Benchmark

T0.4 of `docs/plans/2026-09-27-memory-layers-design.md` (§4, §6). Lets the
Agent Memory Benchmark (AMB, github.com/vectorize-io/agent-memory-benchmark)
evaluate `NativeMemoryStore` — our own `modules/tool-memory` substrate,
used directly (no daemon) — head to head with AMB's own built-in providers.

## No-license constraint (read this first)

**AMB ships no `LICENSE` file.** Per `docs/research/2026-09-27-memory-
systems-gene-survey.md` and D6 in `PROVENANCE.md`, nothing from AMB is
vendored into this repository: no copied source, no copied prompts, no
copied fixtures. `amplifier_memory_provider.py` only *subclasses* AMB's
`MemoryProvider` ABC at runtime, against an externally installed/cloned
copy of AMB that you provide. `run.py` mutates AMB's own `REGISTRY` dict
in memory; it never edits or ships any AMB file. Treat AMB as a **dev-only
external tool**, the same way you'd treat a pinned CLI you don't own the
source of.

## What's here

- `amplifier_memory_provider.py` — `AmplifierMemoryProvider(MemoryProvider)`.
  Each AMB `user_id` becomes its own wing (`amb_{user_id}`); each document's
  first tag (or `"default"`) becomes its room. The backing store never
  touches `~/.amplifier/memory` — it always lives in an isolated, throwaway
  home (AMB's own per-run `store_dir`, or a fresh `tempfile.mkdtemp` when
  constructed directly, as the smoke test does).
- `run.py` — registers `AmplifierMemoryProvider` under the name
  `amplifier-memory` in AMB's `memory_bench.memory.REGISTRY`, then hands off
  to AMB's own `amb run` CLI (`memory_bench.cli:app`) with your args
  passed straight through.
- `tests/test_provider_smoke.py` — **offline**, no AMB install required,
  no network. Ingests 5 documents across 2 users, forces `embedder="none"`
  (lexical-only — no model download), and checks exact-identifier recall +
  user isolation.

## Installing AMB (external dev dependency, not vendored)

Pick one:

```bash
# (a) run.py finds it on sys.path directly — one-shot, no persistent install:
uv run --with git+https://github.com/vectorize-io/agent-memory-benchmark \
    python benchmarks/amb/run.py run --dataset longmemeval --split s --memory amplifier-memory

# (b) clone it once, point AMB_PATH at the checkout root:
git clone https://github.com/vectorize-io/agent-memory-benchmark /tmp/amb
AMB_PATH=/tmp/amb python benchmarks/amb/run.py run --dataset longmemeval --split s --memory amplifier-memory
```

`run.py` also needs `amplifier_module_tool_memory` and `amplifier-data`
importable — use the same interpreter as `modules/tool-memory`'s own test
suite (see `project-context/HANDOFF.md` for which shared dev venv has
`amplifier-data` with `RUST_AVAILABLE=True`).

## Verified AMB interface (2026-09-27, read from source, not docs)

Confirmed by cloning AMB HEAD to a scratch dir and reading:

| Thing | Source | Shape |
|---|---|---|
| `MemoryProvider` | `src/memory_bench/memory/base.py` | class attrs `name`/`description`/`kind`/`concurrency`/`supports_filters`; optional `initialize()`, `cleanup()`, `prepare(store_dir: Path, unit_ids: set[str] \| None, reset: bool = True)`; abstract `ingest(documents: list[Document]) -> None`, `retrieve(query, k=10, user_id=None, query_timestamp=None, filters=None) -> tuple[list[Document], dict \| None]` |
| `Document` | `src/memory_bench/models.py` | `id`, `content`, `user_id`, `messages`, `timestamp`, `context`, `source_ids`, `tags` |
| `REGISTRY` | `src/memory_bench/memory/__init__.py` | `dict[str, type[MemoryProvider]]`; `get_memory_provider(name)` does `REGISTRY[name]()` — **no constructor args**, so registry-mediated runs configure via env vars only |
| CLI entry | `src/memory_bench/cli.py` (`amb`/`omb` console scripts → `memory_bench.cli:app`) | `amb run --dataset <name> --split <name> --memory <name> [--query-limit N] ...` |

**Deviations from the task brief**, found by reading source rather than assuming:

- The CLI flag is **`--split`, not `--domain`**. Datasets declare their own
  split names (see table below); there is no `--domain` flag anywhere in
  AMB's CLI.
- `retrieve()` takes an additional optional `filters: dict | None` parameter
  (only forwarded to providers that set `supports_filters = True`). This
  adapter doesn't set that flag, so `filters` is accepted and ignored —
  same as every other non-filtering provider in AMB's own registry.
- Answer/judge model **defaults confirmed from source**
  (`src/memory_bench/llm/__init__.py`): `OMB_ANSWER_LLM` defaults to
  `groq`, `OMB_JUDGE_LLM` defaults to `gemini` — matching the survey's
  "Groq gpt-oss-120b / Gemini" statement. AMB's CLI *always* requires
  `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) even if you don't change the judge,
  because `_resolve_gemini_key()` runs unconditionally at the top of `run`.

## Exact commands (confirmed dataset/split names from source)

```bash
export GROQ_API_KEY=...      # answer model (OMB_ANSWER_LLM default: groq)
export GEMINI_API_KEY=...    # judge model (OMB_JUDGE_LLM default: gemini) — required unconditionally
# No Groq key? AMB's answer model defaults to groq -- use Gemini for both:
export OMB_ANSWER_LLM=gemini OMB_JUDGE_LLM=gemini

# LongMemEval-S
AMB_PATH=/path/to/agent-memory-benchmark python benchmarks/amb/run.py run \
    --dataset longmemeval --split s --memory amplifier-memory

# LoCoMo
AMB_PATH=/path/to/agent-memory-benchmark python benchmarks/amb/run.py run \
    --dataset locomo --split locomo10 --memory amplifier-memory

# PrecisionMemBench (no LLM in the loop -- needs --mode retrieval)
AMB_PATH=/path/to/agent-memory-benchmark python benchmarks/amb/run.py run \
    --dataset precisionmembench --split single-turn --memory amplifier-memory \
    --mode retrieval
```

Useful flags during iteration: `--query-limit N` (small smoke runs before
spending API budget on the full split), `--oracle` (ingest only gold
documents, isolates answer-generation quality from retrieval quality).

## Embedder modes

- `AMPLIFIER_AMB_EMBEDDER=auto` (default) — tries `FastEmbedEmbedder`
  (local ONNX, same model as the production daemon); falls back to
  lexical-only silently if the model can't load (no network, no cache).
- `AMPLIFIER_AMB_EMBEDDER=none` — always lexical-only, never even attempts
  to load a model. Use this for CI / fully offline smoke runs (that's what
  `tests/test_provider_smoke.py` does, via the constructor kwarg directly
  rather than the env var, since it instantiates the provider itself).

## Retrieval knobs (env vars)

- `AMPLIFIER_AMB_FUSION=rrf` (default) or `legacy` — passed to
  `NativeMemoryStore.search(fusion=...)`. `legacy` is the pre-RRF
  `0.85*cosine + 0.15*lexical` re-rank.
- `AMPLIFIER_AMB_LAYERS=drawer` (default; comma-separated, `drawer`/`fact`) —
  passed as `search(layers=...)`. The benchmark only ingests drawers; facts
  need the distiller, which AMB never runs.
- `AMPLIFIER_AMB_TRACE=/path/trace.jsonl` — appends one JSON line per
  `retrieve()` (`user_id`, `query`, `k`, `fusion`, `granularity`, `rerank`,
  `ids`). AMB saves results with `raw_response=None`, so this trace is the
  only way to compute R@5/R@10 against each query's `gold_ids`.
- `AMPLIFIER_AMB_GRANULARITY=passage` (default) or `drawer` — passed as
  `search(granularity=...)` **only when the store's own signature accepts
  it** (an `inspect.signature` guard, T6.3): the pinned substrate may not
  have T6.1/T6.2's passage-index support yet, and this degrades silently to
  whatever the store returns by default rather than raising. When passage
  hits come back, a passage's own `ref` addresses the passage cell, not the
  parent drawer — `retrieve()` maps it back to AMB's own doc id via
  `drawer_ref` (a mapping built during `ingest()`), and concatenates
  multiple passages of the same parent, in span order, into ONE `Document`
  (separated by `"\n…\n"`) so `gold_ids` recall still keys on AMB's doc id.
- `AMPLIFIER_AMB_RERANK=off` (default) or `on` — passed as
  `search(rerank=...)`, same signature guard as `granularity` above.
- `AMPLIFIER_AMB_DATES=on` (default) or `off` — prefixes each returned
  `Document`'s content with `[YYYY-MM-DD]`, taken from the hit's
  `observed_at` (preferred) or `filed_at` field; a hit with neither gets no
  prefix. No provenance tag (the `(from <drawer_ref>)` suffix
  hooks-memory-briefing/interject add) is included here — AMB's own
  gold_ids/answer-model comparisons should see plain dated text.

`run.py` mixes AMB's real `MemoryProvider` ABC into the adapter at
registration time (the adapter module itself never imports AMB, so the smoke
test runs without it); without that the runner fails on `initialize()` /
`async_retrieve()`.

`REGISTRY`-mediated runs (i.e. every real `amb run ... --memory
amplifier-memory` invocation) can only configure the embedder mode through
the env var, since AMB's harness instantiates the class with no
constructor arguments.

## What to record (per §6 of the design doc)

For each run, record in `project-context/EXPERIMENT_JOURNAL.md`:

- **R@5, R@10** — recall against each query's `gold_ids`, as AMB itself
  computes and reports it.
- **QA accuracy** — the judge-scored accuracy AMB's own summary table
  reports (`EvalSummary.accuracy`).
- **Context tokens per answer** — `QueryResult.context_tokens` (AMB counts
  with tiktoken `cl100k_base`, despite its model comment saying chars/4 — do
  not recompute independently).
- **p95 retrieve ms** — derive from `QueryResult.retrieve_time_ms` across
  the run's queries (AMB records wall time for `memory.retrieve()` only,
  per query).
- Which dataset/split, `--oracle` or not, `AMPLIFIER_AMB_EMBEDDER` mode,
  and the exact AMB commit/version used (`git -C $AMB_PATH rev-parse HEAD`).

Follow the Experiment Journal's existing format (hypothesis / method /
results / conclusion) — see the file's own entries for the template.

## What this task does NOT do

Per the acceptance criteria in `docs/plans/2026-09-27-memory-layers-
design.md` (T0.4): **the real benchmark is not run here** — it needs paid
API keys (Groq + Gemini) and costs money per run. Only the offline smoke
test (`tests/test_provider_smoke.py`) is exercised as part of this task; an
actual `amb run` against LongMemEval-S / LoCoMo / PrecisionMemBench is a
separate, explicit experiment, recorded in `EXPERIMENT_JOURNAL.md` when run.
