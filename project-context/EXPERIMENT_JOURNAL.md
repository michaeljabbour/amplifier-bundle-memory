# Experiment Journal


## 2026-09-28 — DTU end-to-end validation of the three-layer build (fb51886)
**Hypothesis:** the layers work in a real Amplifier session, not just unit tests.
**Method:** DTU `memory-layers-e2e` (Anthropic provider), bundles composing
foundation + memory + memory-index (→ memory-reflect), one with a low
compaction threshold; nine scripted checks run by the validator agent.
**Results:** 9/9 PASS (see PROVENANCE D28). First validation of 5592400
failed 2 checks: briefing never reached the model (listened only on
`session:start`) — fixed by integrating main's prompt:submit delivery (D27);
capture "failure" was a test input outside the category allowlist.
**Learned:** composition-level defects (relative agent tool sources, D25;
session:start injection discarded) are invisible to module suites — keep a
DTU pass in every release.

## 2026-09-28 — D13 retrieval baseline on AMB: RRF vs legacy fusion (L1 path)

**Hypothesis:** RRF fusion (T1.2/D9) beats the legacy
`0.85*cosine + 0.15*lexical` re-rank on retrieval recall (R@5/R@10) and on
judged QA accuracy.

**Method:**

- Code: this repo at `a6a74ef` + uncommitted `benchmarks/amb/` changes (below);
  AMB `03c1d0f` (cloned to a scratch dir, never vendored); amplifier-data
  `2ecdce3` (bm25 lens); amplifier-core `92f2264`; fastembed 0.8.1
  (`all-MiniLM-L6-v2`, 384-d, local); google-genai 2.25.0. Separate venv
  (`uv venv /tmp/amb-venv`), so `~/dev/.venv` was not touched.
- Adapter: `AMPLIFIER_AMB_EMBEDDER=auto` (embedder verified ready),
  `AMPLIFIER_AMB_LAYERS=drawer`, `AMPLIFIER_AMB_FUSION=legacy|rrf`,
  `AMPLIFIER_AMB_TRACE=<file>` (AMB saves `raw_response=None`, so R@k against
  `gold_ids` is computed from this per-query id trace).
- Models: answer `gemini:gemini-2.5-flash-lite` (`OMB_ANSWER_LLM=gemini`),
  judge `gemini:gemini-2.5-flash-lite` (`OMB_JUDGE_LLM=gemini`); temperature 0.
- Commands (env: `PYTHONPATH=modules/tool-memory`, the vars above):
  ```
  /tmp/amb-venv/bin/python benchmarks/amb/run.py run --dataset precisionmembench \
      --split single-turn --memory amplifier-memory --mode retrieval --name amp-$F
  /tmp/amb-venv/bin/python benchmarks/amb/run.py run --dataset longmemeval --split s \
      --memory amplifier-memory --name lme-$F          # resumed with --skip-ingested
  ```
- Latency: in-run AMB numbers are confounded (see caveats), so retrieve
  latency and clean R@k were re-measured with no LLM and nothing else running:
  (A) a fresh store per question, 500 questions, `k=10` as in RAG mode;
  (B) the full 500-question store from the RRF run (316 MB log, ~24k
  drawers), first 40 questions.
- The run exposed three adapter defects, fixed before any recorded number:
  the class never mixed in AMB's `MemoryProvider` (runner failed on
  `initialize()`), the store path was a directory (the log must be a file),
  and a non-empty `raw_response` replaced the retrieved text in AMB's
  LongMemEval/LoCoMo prompts with `json.dumps(raw)` (scores only → 0% QA).
  The adapter now returns `raw_response=None`; diagnostics live on `last_raw`.

**Results:**

| Metric | legacy | rrf | Note |
|---|---|---|---|
| PMB R@5 / R@10 (50 cases with gold) | 0.924 / 0.941 | 0.951 / 0.970 | k=20, retrieval mode |
| PMB mean precision / recall (AMB) | 0.056 / 1.000 | 0.055 / 0.988 | AMB's bm25: 0.054 / 0.965 |
| PMB passes (active) | 9/77 (0/43) | 9/77 (0/43) | no relevance cutoff: always 20 of 35 |
| PMB retrieve p50 / p95 ms | 12.0 / 14.5 | 11.6 / 13.5 | ingest 0.3 s (35 docs) |
| LME-S QA accuracy (500) | 55.8% (279) | 58.2% (291) | McNemar 47 vs 35 discordant, χ²=1.48, p≈0.22 |
| LME-S R@5 / R@10, fresh store per q (A) | 0.912 / 0.958 | 0.928 / 0.970 | 479 q with gold |
| LME-S R@5 / R@10, as run (shared store) | 0.821 / 0.874 | 0.838 / 0.871 | confounded, see caveats |
| LME-S context tokens / question | 28,869 | 28,838 | tiktoken, 10 whole sessions |
| Retrieve p50 / p95 ms, fresh store (A) | 32 / 38 | 105 / 130 | ~50 sessions per store |
| Retrieve p50 / p95 ms, 316 MB store (B) | 8,205 / 8,694 | 8,640 / 9,155 | first call 11 s / 32 s |
| R@10, same 40 q: small vs 316 MB store | 0.950 → 0.825 | 0.975 → 0.875 | cross-wing dilution |
| Ingest, 500 haystacks | 303 s | 299 s | embedding-dominated |

Per-type QA (legacy → rrf): multi-session 49.6 → 57.9, knowledge-update
60.3 → 64.1, single-session-assistant 92.9 → 96.4, single-session-user
78.6 → 77.1, temporal-reasoning 38.3 → 36.8, single-session-preference
26.7 → 23.3.

Reference only, not like-for-like: AMB's published hybrid-search run on
LME-S is 74.0% with answer model `gemini-3.1-pro-preview` (same flash-lite
judge), 23.2k context tokens, and a 1.6 s mean retrieve.

Cost/time: AMB reports no token usage. Estimated ~1,000 Gemini calls plus
retries per fusion, ~15M input tokens per fusion (context-dominated). Wall:
RRF 3 h 02 m; legacy about the same across resumes. The two ran concurrently.
The 50-question pilot projected ~25 min per fusion; it missed flash-lite's
unparseable-output stalls (~2 min per retry, up to ~15 min per query).

**D13 bar (legacy fusion as the v2.0.1 retrieval proxy):**

- LME-S QA ≥ v2.0.1 + 5: **unmet** (+2.4 pts, not significant).
- LoCoMo QA ≥ v2.0.1 + 5: **unmeasured** (not run).
- QA ≥ AMB hybrid baseline: **unmeasured like-for-like**. The published number
  uses a much stronger answer model. Nominally 58.2 vs 74.0.
- PMB R@10 ≥ v2.0.1: **met** (0.970 vs 0.941).
- ≤ 50% context tokens: **unmet** (ratio ≈ 1.0; fusion does not change k or
  payload size).
- Hot-path p95 ≤ 300 ms, zero LLM calls: **met at per-question store scale**
  (130 ms, no LLM in `search`). **Unmet at 316 MB / ~24k drawers** (~9.2 s,
  both fusions, dominated by per-call log folding rather than fusion).

**Caveats:**

- AMB exercises only the L1 drawer retrieval path. Facts/L2 need the
  distiller, which AMB never runs, so they are **not measured here**. Legacy
  fusion approximates v2.0.1 *retrieval*, not its briefing.
- AMB calls `prepare()` once per run, so every question's haystack shares one
  store. Resuming with `--skip-ingested` still resets it (`reset=not
  skip_ingestion`). So RRF ran on one growing store, and legacy's store was
  wiped at each resume (after q91, q499), which spared legacy some
  cross-wing dilution. In-run R@k and latency are confounded; the QA
  comparison, if anything, favours legacy.
- Legacy crashed twice. One crash was a deterministic unparseable answer on
  `d23cf73b` (AMB raises instead of scoring the query wrong). That single
  question was answered with `gemini-2.5-flash` (`--unit`, separate run name,
  row merged) and was judged correct. Counted as wrong, legacy is 55.6%. The
  other crash was a transient `RemoteProtocolError`, resumed.
- MiniLM truncates at 256 tokens, and each LME document is a whole session
  (~2–3k tokens), so the semantic arm sees only each session's opening.
- `layers=("drawer",)` widens the internal pool to `max(3k, 50)`. The
  default both-layers path requests exactly k, so its latency may be lower.
- The pilot outputs were discarded (two pilot processes overlapped); raw
  outputs stay in `/tmp/amb-runs` (not in the repo).

**Conclusion:** RRF is the better L1 ranker. It gives higher recall on both
suites (+1.6 R@5 on LME-S, +2.9 R@10 on PMB) and +2.4 QA, with the largest
gain on multi-session questions (+8.3). But the QA gain is not significant,
and it is well short of D13's +5 bar. Neither fusion reduces context. The two
blocking findings are about the store, not fusion: scoped search loses recall
as unrelated wings accumulate (R@10 0.975 → 0.875 for the same questions),
and p95 grows to ~9 s at 316 MB. Next: find the dilution mechanism (unverified
hypothesis: candidates or BM25 statistics are drawn store-wide and scoped
after ranking), bound per-call fold cost, run LoCoMo, and run AMB's
hybrid provider under the same flash-lite answer model before claiming any
comparison with it.

## 2026-08-24 — large-store search hot path and daemon multiplicity

**Hypothesis:** the user-visible TUI stalls come from synchronous memory
retrieval and duplicate daemons, not from Textual rendering.

**Method:** sampled the live TUI process, timed the memory daemon's health and
search surfaces, correlated tool-call durations in the affected session, and
benchmarked the old regenerate-per-hit search against the one-fold
implementation on the same 68,894-event store. Process enumeration checked how
many daemons shared `~/.amplifier/memory`.

**Results:**

- The TUI main loop was idle in 88% of sampled stacks; its event ledger was
  2.2 MB / 1,770 records, so render/replay volume did not explain the stalls.
- Daemon health returned in 13 ms, while search exceeded 20 seconds before the
  hot-path fix. In the affected turn, 41 of 45 non-delegate tool calls took
  14–17 seconds (median 15.154 seconds), matching the 15-second client timeout.
- Search on the 68,894-event store improved from 42.03 seconds to 1.85 seconds
  when the log was folded once per query instead of once per result.
- Ten daemon processes shared the same memory home; the hottest used roughly
  3.14 GB RSS and a full CPU core.

**Conclusion:** the causal chain is confirmed: per-tool synchronous retrieval
amplified an O(results x log-fold) search, while the spawn-only lock allowed
duplicate long-lived daemons. Keep semantic retrieval at prompt/orchestrator
boundaries by default, bound it to 3 seconds, retain the one-fold search, and
enforce a kernel-held lifetime singleton. These changes preserve retrieval
result equivalence and fail open when the memory service is slow.

## 2026-06-07 — ANN + embedder spike: can the vector index move to amplifier-data?

**Hypothesis:** amplifier-data's vector lens can replace the legacy vector
backend as memory's semantic index without regressing retrieval quality (the
legacy vendor store reports 96.6% R@5 on LongMemEval).

**Method (honest proxy — NOT full LongMemEval):** I do not have the LongMemEval
dataset/harness, so I measured retrieval-equivalence on a REAL corpus instead.
- Corpus: 143 real drawers mined by the legacy vendor store from this bundle's docs.
- Embedder: the legacy vendor store's own EmbeddingGemma ONNX, 384-dim
  — the SAME embeddings fed to both indexes (apples-to-apples).
- Index A: amplifier-data `add_embedding` + `query_vector` (exact brute-force cosine).
- Index B: the legacy vector backend's HNSW (cosine) — the incumbent.
- Quality: self-retrieval recall@5 + top-5 agreement over 40 queries.
- Speed: p50 query latency vs corpus size, padded with random 384-d vectors.
- Env: a venv with the legacy vendor store + its vector backend, plus
  amplifier-data via sys.path (RUST_AVAILABLE=True). Scripts: `/tmp/spike.py`,
  `/tmp/spike_speed.py`.

**Results — QUALITY (zero regression):**
```
self-recall@5  amplifier-data(exact)      : 40/40 = 100.00%
self-recall@5  legacy vector backend(HNSW): 40/40 = 100.00%
top-5 agreement (avg overlap)             : 100.00%
```
amplifier-data's exact cosine reproduces the legacy vector backend's HNSW
results EXACTLY on identical embeddings. The index backend itself does not
cost any recall.

**Results — SPEED (the real gap, O(N) vs flat):**
```
      N   amplifier-data p50(ms)   legacy vector backend p50(ms)
    143                     5.28               0.29
   1000                    39.81               0.50
   5000                   204.19               0.77
  10000                   447.66               0.94
  20000                   893.38               0.93
```
amplifier-data brute-force is linear (~0.045 ms/vector); the legacy vector
backend's HNSW stays sub-millisecond regardless of N.

**Conclusion / decision:** The index CAN move on QUALITY (zero regression) but
CANNOT move on PERFORMANCE until amplifier-data gains an ANN lens (HNSW/IVF).
The interject hook fires multiple times per turn and a long-lived store reaches
10k+ drawers, where 448 ms/query brute-force is unusable on the hot path.
→ **Keep the legacy vector backend as the semantic index; use amplifier-data
for the verbatim + KG + scope floor (the dual-write architecture already
built).** Revisit a full index move only when amplifier-data ships an ANN
lens. R@5 is not threatened by the backend choice — latency is.

**Caveat:** This is a 143-drawer equivalence proxy, not a LongMemEval R@5
reproduction (needs the dataset + harness). The quality result is content-
limited; the speed result is definitive (content-agnostic, O(N) is structural).

*(External research record for the full pre-scrub text of this entry:
memthoughts/research/amplifier-memory-v2.1/lineage.md, not part of this
bundle.)*
