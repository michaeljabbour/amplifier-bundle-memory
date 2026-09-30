# Experiment Journal

## 2026-09-30 — P8 follow-up: D13 campaign #4 (context expansion: rank on passages, read on neighbourhoods)

**Hypothesis (D34):** C misses multi-session answers because a few ~300-token
passages leave out details inside sessions it already retrieves. If ranking
stays on passages but the top hits are read with more of their session
(neighbour passages or the whole drawer), capped at 10–14k tokens, LME-S QA
should reach A + 5 (60.8%) and the hybrid baseline (62.2%) without losing
temporal QA or LoCoMo.

**Method:**

- Code: `da67607` (branch `feat/memory-layers`), plus one uncommitted
  harness-only change (see "Harness defect found"). AMB `03c1d0f`, venv
  `/tmp/amb-venv` (pytest and pytest-asyncio had to be reinstalled; the adapter
  suite gives 40 passed). Answer and judge were both `gemini-2.5-flash-lite`,
  with the same robustness settings as campaign #3 (`RETRY_DISCONNECTS=on`,
  `REQUEST_TIMEOUT_S=90`, `FALLBACK_MODEL=gemini-2.5-flash`; "strict" counts
  fallback-answered rows as wrong). Scripts and outputs are in
  `/tmp/amb-runs/t68/`: `cfg.sh`, `drive.sh`, `clone.sh`, `chain1-3.sh`,
  `analyze.py`, `fb.py`, `rall.py`, `mcn.py`, plus `warmrep.py`, a no-LLM
  replay that opens a store, runs the store's own eager build (as the daemon
  does, D33), then times `retrieve()` through the adapter. Jobs ran one at a
  time under nohup. Host load average was 4–13 from unrelated work.
- Configs: all use rrf, passage granularity, dates on, rerank off, MiniLM and
  `PACK=k`.
  - **C**: `EXPAND=none` (same-day reference)
  - **N1**: `EXPAND=neighbors NEIGHBORS=1 EXPAND_TOKENS=10000`
  - **N2**: `EXPAND=neighbors NEIGHBORS=2 EXPAND_TOKENS=14000`
  - **D4**: `EXPAND=drawer EXPAND_TOKENS=14000`
  - **G**: D4 + `EXPAND_ONLY_AGGREGATE=on` (47 of the 100 subset queries
    matched a cue)
- Reuse of ingested stores: expansion does not change ingestion. Every
  subset config ran with `--skip-ingestion` on an APFS clone of campaign #3's
  `sub-C` store. Full LME-S D4 ran on a clone of campaign #3's full `lme-C`
  store (1.5 GB, 500 wings). LoCoMo D4 was ingested fresh (16 s). So C and D4
  differ only in retrieval and rendering. Same-day C on the subset reproduced
  campaign #3's C (56 vs 57, one discordant question).
- Latency is reported two ways. **As-run** is the harness's retrieve time on
  a reopened store: the first query pays the cold build and each wing's first
  query builds its partition. **Warm** comes from `warmrep.py` after the eager
  build.

**Harness defect found (fixed, uncommitted, output-identical):** in the
first N1 and N2 runs, retrieve took 13–18 s per query. By contrast, C took
155 ms and the store's own `search(expand=...)` took about 4 ms. The profile
showed that `_render_expanded_group` in the adapter called
`store._payload_text(drawer_ref, None)`. With no fold available, every
drawer went through a kernel `get_cell`, which regenerates the whole log:
2.0–2.7 s per drawer on the 0.3 GB store, dominated by 5M event re-hashes. The
fix passes the store's fold snapshot, which is what the store's own expansion
already does. Refs are content-addressed and six sampled drawers were
byte-identical on both paths, so the rendered context and the subset QA
results do not change. Only latency does. N1, N2 and D4 ran before the fix,
and G and all full runs ran after it. Product note: a `_payload_text` miss
without a fold costs O(log) per call, not "cheap on repeat" as its docstring
says. The product's briefing reads `hit["context"]` and does not take this
path.

**Results — subset (100 LME-S questions; choice made here only):**

| Config | QA (strict) | multi-sess /27 | temporal /27 | KU /15 | SSU /14 | SSA /11 | pref /6 | R@5 / R@10 | ctx tok/q | as-run p50 / p95 ms | warm p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C | 56 (56) | 11 | 10 | 12 | 12 | 8 | 3 | 0.944 / 0.949 | 2,362 | 155 / 169 | 5.0 / 6.7 |
| N1 | 58 (58) | 13 | 7 | 14 | 13 | 11 | 0 | same | 5,715 | 14,420 / 18,409 † | 5.2 / 7.0 |
| N2 | 64 (64) | 14 | 9 | 13 | 14 | 11 | 3 | same | 7,294 | 13,996 / 18,199 † | 5.0 / 6.7 |
| **D4** | **66 (66)** | 12 | **14** | 14 | 14 | 11 | 1 | same | 9,026 | 6,282 / 11,793 † | 4.9 / 6.5 |
| G (D4, aggregate-gated) | 61 (61) | 11 | 13 | 14 | 12 | 8 | 3 | same | 5,443 | 159 / 174 | 4.8 / 6.6 |

† Before the harness fix. Warm numbers are after the fix, from one warm
process over the same store (eager build 32 s, 100 wings). No subset run had
a fallback or unparseable row. Ranking is identical across configs:
per-type recall and coverage from `rall.py` match C exactly. D4 expanded
2.8 of about 6 documents per query on average before the 14k cap. N1 and N2
expanded all of them.

Paired against C: N1 10 vs 8 discordant (p=0.81), N2 16 vs 8 (p=0.15),
D4 20 vs 10 (p=0.099). G vs D4: 6 vs 11 (p=0.33).

**Choice:** D4. It had the highest QA and was the only expanded config with
temporal QA at or above C's (14 vs 10). Both neighbour windows lost temporal
questions. G, the benchmark-shaped gated variant, scored 5 below D4 and was
not chosen. The unconditional config is preferred anyway.

**Results — full LME-S (500), D4 vs campaign #3's references:**

| Metric | A (legacy, 2026-09-28) | C (campaign #3) | H hybrid (campaign #3) | **D4** |
|---|---|---|---|---|
| QA | 55.8% | 59.4% (strict 59.0) | 62.2% (strict 61.4) | **71.8% (strict 70.8)** |
| knowledge-update /78 | 47 | 55 | 58 | 65 |
| multi-session /133 | 66 | 51 | 77 | 74 |
| single-session-assistant /56 | 52 | 50 | 56 | 55 |
| single-session-preference /30 | 8 | 10 | 6 | 9 |
| single-session-user /70 | 55 | 53 | 63 | 62 |
| temporal-reasoning /133 | 51 | 78 | 51 | **94** |
| R@5 / R@10 (479 with gold) | 0.912 / 0.958 | 0.955 / 0.967 | — | 0.953 / 0.965 ‡ |
| context tok/q | 28,869 | 2,368 | 23,360 | 9,097 (31.5% of A) |
| retrieve p50 / p95 ms, as-run | — | 7.7 / 12.9 (in-process ingest) | 791 / 1,537 | 197 / 230 (first query 656 s cold build) |
| retrieve p50 / p95 ms, warm | — | 5.2 / 6.7 (same replay) | — | **5.5 / 7.0** |
| fallback / unparseable rows | — | 2 / 0 | 5 / 0 | 6 / 0 |

‡ Measured on the reopened store. The ranking config is the same as C's, so
the −0.002 gap is a reopen-versus-in-process difference and was not
investigated. Warm replay on the full store: open 16 s, eager build 776 s
(500 wings), then 500 queries per config.

Paired tests (McNemar, exact two-sided):

- **D4 vs A:** 359 vs 279, with 108 questions only D4 got right and 28 only A
  got right; χ²=45.9, **p<1e-6**. Strict: 354 vs 279, 104 vs 29, p<1e-6.
  Per type: temporal +43 (p<1e-6), knowledge-update +18 (p=0.0005),
  multi-session +8 (p=0.26).
- **D4 vs H:** 359 vs 311, with 89 only-D4 and 41 only-H; χ²=17.0,
  **p<0.001**. Strict: 354 vs 307, 86 vs 39, p=3e-5. Multi-session 74 vs 77
  (p=0.76), so D4 closes the gap and the difference is no longer
  significant. Temporal 94 vs 51 (p<1e-6).
- **D4 vs C:** 359 vs 297, 99 vs 37, χ²=27.4, p<1e-6. Multi-session +23
  (p=0.0004), temporal +16 (p=0.017), single-session-user +9 (p=0.035).

**Results — full LoCoMo (1,540), fresh ingest:**

| Metric | A | C (D31) | H | **D4** |
|---|---|---|---|---|
| QA | 67.7% | 81.4% (strict 81.3) | 74.5% (strict 73.9) | **83.5% (strict 82.7)** |
| temporal /321 | — | 245 | — | 253 |
| multi-hop /96 | — | 60 | — | 68 |
| single-hop /282 | — | 190 | — | 198 |
| open-domain /841 | — | 758 | — | 767 |
| R@5 / R@10 (session) | 0.511 / 0.695 | 0.820 / 0.877 | — | 0.820 / 0.877 |
| context tok/q | 14,678 | 2,918 (19.9%) | 22,253 | **11,072 (75.4% of A)** |
| retrieve p50 / p95 ms | 17 / 29 | 10 / 60 | 47 / 72 | 10 / 17 |
| fallback rows | 34 | 3 | 13 | 15 |

D4 vs C: 117 vs 84 discordant, p=0.024 (strict 116 vs 94, p=0.15). D4 vs
H: 256 vs 117, p<1e-6. D4 vs A: 344 vs 100, χ²=133. No LoCoMo category went
down.

**D13 bar (A = legacy proxy for v2.0.1; candidate = D4):**

| Item | Bar | D4 | Verdict |
|---|---|---|---|
| (a) LME-S QA ≥ A + 5 | ≥ 60.8% | 71.8% (strict 70.8), +16.0, p<1e-6 | **met** |
| (b) LoCoMo QA ≥ A + 5 | ≥ 72.7% | 83.5% (strict 82.7), +15.8, χ²=133 | **met** |
| (c) QA ≥ hybrid baseline, like-for-like | LME-S 62.2 / LoCoMo 74.5 | 71.8 (p<0.001) / 83.5 (p<1e-6) | **met on both** |
| (d) PMB R@10 ≥ 0.941 | 0.941 | 0.970 (D31); not re-run | **met** (expansion changes rendering, not ranking; subset and LoCoMo R@k are identical to C's) |
| (e) context tokens ≤ 50% of A | ≤ 50% | LME-S 31.5%; **LoCoMo 75.4%** | **met on LME-S, unmet on LoCoMo** |
| (f) retrieve p95 ≤ 300 ms at full store, zero LLM calls | 300 ms | warm 7.0 ms on the 1.5 GB store; as-run 230 ms on a reopened store | **met** after the one-time eager build (776 s) |

**Caveats:** the candidate was chosen on the fixed subset only, and the full
runs were confirmations. A is still the 2026-09-28 run. H and C (full) come
from campaign #3 on the same store and harness. The per-type tests are
uncorrected. The (e) failure on LoCoMo comes from the fixed 14k cap: LoCoMo
sessions are short, so whole drawers fit and the cap is rarely reached. The
subset as-run latencies for N1, N2 and D4 include the harness defect above.
Only L1 is exercised.

**Conclusion:** reading the parent drawer of the top-ranked passages is the
lever. On LME-S, QA rose from 59.4 to 71.8. Multi-session recovered to H's
level (74 vs 77), and temporal gained further (94). (a) and (c) are now met
on both suites and LoCoMo did not regress (83.5 vs 81.4). The remaining
unmet item is (e) on LoCoMo, at 75% of A's tokens. Next lever: make the
expansion budget relative rather than absolute. Two options, both tuned on
subsets only: cap expansion at a multiple of the unexpanded context, or at
half of what whole-session context would cost for that query. Then re-check
LoCoMo QA at ≤ 7.3k tokens per question. Also commit the adapter fold fix,
and consider making `_payload_text` fall back to a fold snapshot rather than
a full-log kernel resolve.

## 2026-09-29 — T7 follow-up: D13 campaign #3 (token-budget packing, full LME-S for C and the hybrid baseline)

**Hypothesis:** C's multi-session deficit on LongMemEval-S (LME-S) comes from
session coverage. Ten passage hits, capped at two per session, reach too few
sessions. Diversity-first packing to a token budget (T7.3), which adds one
passage per session before any second passage, should recover multi-session
QA without losing temporal QA. That would lift LME-S QA to at least A + 5
(60.8%).

**Method:**

- Code: `86b8700` (branch `feat/memory-layers`), with no changes. AMB is
  `03c1d0f` and the venv is `/tmp/amb-venv`. Answer and judge were both
  `gemini-2.5-flash-lite`. Every run in this entry used the robustness
  settings: `RETRY_DISCONNECTS=on`, `REQUEST_TIMEOUT_S=90`,
  `FALLBACK_MODEL=gemini-2.5-flash`. "Strict" scores count two kinds of row
  as wrong: rows answered by the fallback model, and unparseable rows that
  were re-answered with 2.5-flash. Scripts and raw outputs are in
  `/tmp/amb-runs/t67/`: `cfg.sh`, `drive.sh`, `chain1.sh`, `chain2.sh`,
  `analyze.py`, `fb.py`, plus new files `rall.py` (recall over every returned
  doc, by type) and `mcn.py` (paired McNemar: exact binomial and χ²). Jobs ran
  one at a time under nohup. The host load average was 6–27 from unrelated
  work.
- Configs: all use rrf, passage granularity, dates on, rerank off and MiniLM,
  with product fold behaviour (no retention override).
  - **C** `AMPLIFIER_AMB_PACK=k` (the campaign #2 winner)
  - **P6** `PACK=budget TOKEN_BUDGET=6000 CANDIDATES=40`
  - **P10** `PACK=budget TOKEN_BUDGET=10000 CANDIDATES=60`
  - **H** AMB's hybrid baseline (`--memory qdrant`), with its dense encoder on
    `mps` and encoder calls serialized, as in campaign #2.
- The subset is the fixed 100 LME-S ids in `lme100.ids` (seed 20260928, see
  the previous entry). C was ingested fresh. P6 and P10 ran with
  `--skip-ingestion` on an APFS clone of C's store, so they differ from C
  only in retrieval.
- Adapter tests: `benchmarks/amb/tests` gave 32 passed, run under
  `/tmp/amb-venv` with pytest and pytest-asyncio. Nothing in
  `benchmarks/amb/` changed.

**Results — subset (100 LME-S questions; choice made here only):**

| Config | QA (strict) | multi-sess /27 | temporal /27 | KU /15 | SSU /14 | SSA /11 | pref /6 | R@5 / R@10 | docs returned | ctx tok/q | retrieve p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **C** | **57 (57)** | 11 | **11** | 12 | 12 | 8 | 3 | 0.944 / 0.952 | 6.1 | 2,364 | 6.7 / 10.2 |
| P6 | 55 (54) | 11 | 9 | 14 | 13 | 8 | 0 | 0.930 / 0.963 | 18–20 | 5,995 | 167 / 179 † |
| P10 | 56 (55) | 12 | 10 | 13 | 12 | 8 | 1 | 0.940 / 0.974 | 26–28 | 9,924 | 168 / 185 † |

† P6 and P10 ran on a reopened store, so these p95s include the post-open
build. They are not comparable to C's in-process ingest-then-query p95.
P6 had 1 unparseable row and P10 had 2; each was re-answered with 2.5-flash.
The fallback model answered 1 row in each of P6 and P10 and none in C.

**Choice:** neither budget config beat C. P10 gained one multi-session
question and lost one temporal question, and P6 lost two temporal questions.
Both were below C overall, within the ±3–7 pt subset noise, at 2.5–4.2× the
tokens. Step 2 therefore ran C. Step 4 (LoCoMo) was skipped because the
chosen config is unchanged (81.4% stands from D31).

**Session-coverage check (falsifies the hypothesis):** recall over every
returned parent doc showed that C already covers the gold sessions. Under C,
**85% of multi-session questions on full LME-S have every gold session in
context**, with only about 6 distinct sessions returned. Of C's 79 wrong
multi-session answers, 60 had all gold sessions present, and H answered 33 of
those 60 correctly. Budget packing lifts multi-session full coverage to 100%
on the subset, but QA does not move. The deficit is **within-session
content**: one to three ~300-token passages from a session miss the details
that a count or aggregate needs. H feeds whole 512-token chunks, top-50
(~23k tokens).

**Results — full LME-S (500):**

| Metric | A (legacy, 2026-09-28) | **C (today)** | H hybrid (today) |
|---|---|---|---|
| QA | 55.8% | **59.4%** (strict 59.0) | **62.2%** (strict 61.4) |
| knowledge-update /78 | 47 | 55 | 58 |
| multi-session /133 | 66 | **51** | **77** |
| single-session-assistant /56 | 52 | 50 | 56 |
| single-session-preference /30 | 8 | 10 | 6 |
| single-session-user /70 | 55 | 53 | 63 |
| temporal-reasoning /133 | 51 | **78** | 51 |
| R@5 / R@10 (479 with gold, by parent doc) | 0.912 / 0.958 ‡ | 0.955 / 0.967 | not recoverable |
| context tok/q | 28,869 | 2,368 (8.2% of A) | 23,360 |
| retrieve p50 / p95 ms | — | 7.7 / 12.9 | 791 / 1,537 |
| ingest | — | 3,986 s (campaign #2: 12,889 s) | 4,475 s (mps) |
| fallback / unparseable rows | — | 2 / 0 | 5 / 0 |

‡ From campaign #2's fresh-store measurement.

Paired tests (McNemar, exact two-sided):

- **C vs A:** 297 vs 279, with 97 questions only C got right and 79 only A
  got right; χ²=1.64, **p=0.20**. The difference is +3.6 pt, and +5.0 is
  needed. Per type: temporal +27 (p=0.0005); multi-session −15 (p=0.063).
- **C vs H:** 297 vs 311, with 73 only-C and 87 only-H; χ²=1.06, **p=0.30**.
  C is 2.8 pt below H, and the difference is not significant. H is
  significantly better on multi-session (+26, p=0.0005), single-session-user
  (+10, p=0.02) and single-session-assistant (+6, p=0.03). C is
  significantly better on temporal (+27, p=0.0003).
- **H vs A:** 311 vs 279, 67 vs 35; p=0.002.
- **C today vs C in campaign #2:** 297 vs 296, with only 15 discordant
  questions. Full-run noise is under 1 pt. The T7.1 and T7.2 changes did not
  move answers.
- H's subset score reproduced within noise: 65 today vs 61 in campaign #2,
  with 8 discordant questions.
- AMB also ships a published hybrid LME-S result of 74.0% (in
  `/tmp/amb-runs/hybrid-lme.json.gz`). Its answer model is
  gemini-3.1-pro-preview, so it is not like-for-like and is not used here.

**D13 bar (A = legacy proxy for v2.0.1; C = shipped config):**

- (a) LME-S QA ≥ A + 5 (≥ 60.8%): **unmet**. C scored 59.4 (strict 59.0),
  +3.6, p=0.20.
- (b) LoCoMo QA ≥ A + 5: **met** (D31). 81.4 vs 67.7, χ²=99. Not re-run
  because the config is unchanged.
- (c) QA ≥ the hybrid baseline, like-for-like: **met on LoCoMo** (D31: 81.4
  vs 74.5, χ²=29). **Unmet on LME-S**: 59.4 vs 62.2 (strict 59.0 vs 61.4),
  −2.8, p=0.30. C is not significantly worse, but it does not reach H.
- (d) PMB R@10 ≥ 0.941: **met** (D31, 0.970).
- (e) Context tokens ≤ 50% of A: **met**. 8.2% on LME-S (re-measured today)
  and 19.9% on LoCoMo (D31).
- (f) Retrieve p95 ≤ 300 ms at full store, zero LLM calls: **met** (D33:
  1.05 GB store, warm p95 48.9 ms, post-idle p95 6 ms). Today's
  in-harness full LME-S run agrees: p95 12.9 ms, compared with 35.5 s in
  campaign #2.

**Caveats:** A is still the 2026-09-28 run and was not re-run the same day.
Full-run reproducibility for C (15 discordant questions) suggests this is
minor. The subset tuning was the only place a choice was made. The
significance tests are per-question paired and uncorrected for the per-type
breakdown. Only L1 retrieval is exercised; L2 facts and the L3 briefing are
not.

**Conclusion:** token-budget packing does not recover multi-session QA,
because session coverage was never the bottleneck. C's passages cover the
right sessions but drop the details inside them, while whole-session context
(A, or H's 512-token chunks at 23k tokens) keeps them. Temporal gains from
passages and dates (+27) are almost fully cancelled by multi-session and
single-session-user losses. That leaves (a) and (c) on LME-S unmet. Next,
tuned on the subset only:

1. **Parent-session expansion.** Keep passage ranking, but render the top N
   sessions (N≈3–4, capped near 14k tokens) as whole drawers, or as
   neighbour-passage windows around each hit. Rank on passages, read on
   sessions.
2. Apply expansion only when the query asks for an aggregate across sessions
   ("how many", "total", "in total", "across"). Otherwise keep C's 2.4k-token
   context.
3. If either moves the subset, run full LME-S once, then LoCoMo to check for
   regression, since LoCoMo's lead came from C's compact context.

## 2026-09-29 — T6.6: D13 campaign on the P6 levers (ablation, full runs, bar verdict)

**Hypotheses (one per lever, from D29):**
passages (T6.2) cut context tokens by roughly 10× and improve QA by removing
noise; date stamps (T6.3) fix temporal questions; the cross-encoder rerank
(T6.4) raises R@5 and QA; bge-small-en-v1.5 (T6.5) beats MiniLM-L6 on recall;
per-wing partitions (T6.1) keep retrieval p95 ≤ 300 ms at full-store scale.

**Method:**

- Code: this repo at `8214191` plus uncommitted changes (listed below); AMB
  `03c1d0f`; amplifier-data 0.2.0 (pin `2ecdce3`); fastembed 0.8.1;
  google-genai 2.25.0; venv `/tmp/amb-venv`. Answer and judge were both
  `gemini-2.5-flash-lite` at temperature 0, the same as the baseline
  (`OMB_ANSWER_LLM=gemini OMB_JUDGE_LLM=gemini`). Raw outputs, logs and
  scripts are in `/tmp/amb-runs/t66/` (not in the repo).
- Jobs ran **one at a time**, each under a fresh run name, through
  `drive.sh NAME DATASET SPLIT IDS` (AMB `run` under nohup). After a crash it
  resumes with `--skip-ingested`. A query that fails to parse is excluded and
  then re-answered with `gemini-2.5-flash` (the baseline protocol). R@k is
  computed by parent doc from `AMPLIFIER_AMB_TRACE` (`analyze.py`).
- Ablation subset: 100 LongMemEval-S questions, stratified by `question_type`
  (proportional, largest remainder), `random.Random(20260928)`. Counts:
  knowledge-update 15, multi-session 27, single-session-assistant 11,
  single-session-preference 6, single-session-user 14, temporal-reasoning 27.
  The ids are in `/tmp/amb-runs/t66/lme100.ids` and are listed here in full:
  e493bb7c 618f13b2 07741c44 4d6b87c8 10e09553 affe2881 7e974930 45dc21b6
  72e3ee87 cf22b7bf f685340e ba61f0b9 06db6396 603deb26 945e3d21 36b9f61e
  1f2b8d4f 681a1674 9aaed6a3 0a995998 85fa3a3f 87f22b4a a9f6b44c a3332713
  gpt4_31ff4165 51c32626 60bf93ed 2b8f3739 a96c20ee_abs 37f165cf eeda8a6d_abs
  gpt4_5501fe77 3c1045c8 e6041065 1a8a66a6 3fe836c9 28dc39ac e25c3b8d d23cf73b
  6456829e 4adc0475 73d42213 0e5e2d1a e8a79c70 eaca4986 fea54f57 e9327a54
  c8f1aeed e982271f 6222b6eb 8752c811 a40e080f cc539528 b6025781 b0479f84
  06f04340 1a1907b4 75832dbd d6233ab6 6b168ec8 001be529 d52b4f67 c14c00dd
  f4f1d8a4 0862e8bf_abs 25e5aa4f 545bd2b5 4100d0a0 21436231 86b68151 1e043500
  15745da0_abs bc8a6e93 71017276 gpt4_fe651585 gpt4_7ca326fa gpt4_7f6b06db
  af082822 9a707b81 gpt4_468eb063 gpt4_d6585ce8 gpt4_4929293b a3838d2b
  gpt4_98f46fc6 gpt4_8c8961ae gpt4_d6585ce9 gpt4_8279ba03 gpt4_e414231f
  0bc8ad92 c8090214_abs gpt4_93159ced 71017277 gpt4_70e84552 5e1b23de
  gpt4_59149c77 gpt4_93f6379c gpt4_e061b84f gpt4_2c50253f 2a1811e2 gpt4_4ef30696
- Configs (adapter env; embedder `auto`, `LAYERS=drawer`, k=10 as AMB RAG
  passes it):
  - **A** `FUSION=legacy GRANULARITY=drawer DATES=off PASSAGES=off` (proxy for v2.0.1)
  - **B** `FUSION=rrf GRANULARITY=drawer DATES=off PASSAGES=off` (the D26 config)
  - **C** `FUSION=rrf GRANULARITY=passage DATES=on PASSAGES=on`
  - **D** C + `RERANK=on`; reuses C's built store via `--skip-ingestion`
  - **E** C + `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`, re-ingested
  - **H** AMB's own hybrid baseline (`--memory qdrant`, registered as
    "hybrid-search"): a 0.6B dense embedder plus a learned sparse arm, RRF,
    512-token chunks, top-50 chunks. It ran as AMB ships it, except that the
    dense encoder ran on `mps` (`AMPLIFIER_AMB_HYBRID_DEVICE`). On `cpu` it
    took ~120 s per 32-chunk batch, about 27 h for the subset. Encoder calls
    were serialized, because concurrent GPU calls aborted the process.
- Command shape (per config, env from `cfg.sh`):
  ```
  AMPLIFIER_AMB_QUERY_IDS=lme100.ids AMPLIFIER_AMB_TRACE=<name>.trace.jsonl \
  /tmp/amb-venv/bin/python benchmarks/amb/run.py run --dataset longmemeval --split s \
      --memory amplifier-memory --name sub-<X> -o /tmp/amb-runs/t66/outputs
  # full: --dataset locomo --split locomo10 | --dataset longmemeval --split s
  # PMB:  --dataset precisionmembench --split single-turn --mode retrieval
  ```
  The full runs also set `AMPLIFIER_AMB_RETRY_DISCONNECTS=on
  AMPLIFIER_AMB_REQUEST_TIMEOUT_S=90 AMPLIFIER_AMB_FALLBACK_MODEL=gemini-2.5-flash`.
  Flash-lite has degenerate generations that run for about 9 minutes, and
  dropped connections that AMB does not retry. Each of these aborted a whole
  LoCoMo conversation, and the first LoCoMo attempt restarted six times. Rows
  answered by the fallback model are counted below, and "strict" scores them
  as wrong. The ablation runs used AMB's own policy: they waited out stalls,
  and one crash was re-answered with 2.5-flash.
- Uncommitted changes made for this run:
  - `benchmarks/amb/`: the adapter now mirrors the daemon's write and search
    path. It passes `embedder` and `embedding_model_id` to `file()` and
    `current_model_id` to `search()`. **Before this change, passages were
    never embedded in the harness.** New env vars:
    `AMPLIFIER_AMB_EMBEDDING_MODEL`, `AMPLIFIER_AMB_PASSAGES`,
    `AMPLIFIER_AMB_FOLD_RETENTION_S`. `run.py` gained
    `AMPLIFIER_AMB_QUERY_IDS` (subset filter) and the resilience and device
    knobs above. Tests were added.
  - **Product fix** in `tool-memory/store.py`, `_split_into_passages`: it cut
    only at line boundaries. Every LongMemEval and LoCoMo session is a single
    line of JSON, so each long drawer became **one drawer-sized passage** and
    T6.2 was inert (smoke run: 26.9k context tokens in passage mode). Lines
    longer than the overlap are now cut at whitespace (`_bounded_lines`).
    Separately, a passage with no natural boundary in the lookahead window was
    cut at the window's end rather than at the target size, contrary to its
    docstring; that is fixed too. The tool-memory suite passes (555), and the
    AMB tests and the name gate pass.

**Results — ablation (LongMemEval-S, the same 100 questions):**

| Config | QA | R@5 / R@10 | Context tok/q | Retrieve p50 / p95 ms (as run) | Ingest |
|---|---|---|---|---|---|
| A legacy/drawer | 52% (strict 51) | 0.890 / 0.927 | 28,342 | 180 / 449 | 63 s |
| B rrf/drawer | 48% | 0.880 / 0.967 | 28,767 | 130 / 255 | 63 s |
| **C rrf/passage/dates** | **57%** | **0.944** / 0.952 | **2,366** | 3,909 / 16,990 † | 2,532 s |
| D C + rerank | 54% | 0.940 / 0.949 | 2,362 | 224 / 19,670 † | (C's store) |
| E C + bge-small | 57% | 0.940 / 0.948 | 2,405 | 3,869 / 15,136 † | 3,472 s |
| H hybrid baseline | 61% | not recoverable ‡ | 23,355 | 223 / 360 | 929 s (mps) |

Per-type QA, A → C (n): knowledge-update 9 → 12 (15), multi-session
12 → 11 (27), single-session-assistant 10 → 8 (11), single-session-preference
2 → 3 (6), single-session-user 13 → 12 (14), temporal-reasoning 6 → 11 (27).
For H: 14, 16, 11, 2, 13, 5.

† Passage-store latency is dominated by re-folding the whole log (see
Latency). ‡ AMB saves `raw_response=None`, and H's rendered context carries
no doc ids.

**Noise floor:** the same config on the same 100 ids does not reproduce
exactly. Legacy scored 49 in the baseline run and 52 here (3 vs 6 discordant
questions). RRF/drawer scored 55 in the baseline and 48 here (B), with 10 vs
3 discordant. The subset cannot resolve QA differences under about 7 points;
choices below lean on R@k, tokens and cost where QA ties.

**Choice (subset only):** C wins. Its QA is tied highest with E, and its
tokens are 8% of A's. E adds 37% ingest time and gains nothing, so the tie
goes to C. Rerank did not help: D is −3 QA versus C (5 vs 2 discordant),
−0.4 R@5, and adds latency. Full runs therefore used A and C. LME-S for A was
not re-run. The estimate for the plan exceeded ~10 h, and full LME-S for C
alone took ~5 h. A's LME-S number is the baseline's 55.8% (legacy path, no
passages), whose 100-question subset reproduces within noise (49 vs 52).

**Results — full runs:**

| Metric | A (legacy) | C (winner) | Hybrid (H) | Note |
|---|---|---|---|---|
| LME-S QA (500) | 55.8% (baseline) | 59.2% (strict 59.0) | not run | vs A: 79 vs 96 discordant, χ²=1.46, p≈0.23 |
| LME-S R@5 / R@10 (479 with gold) | 0.912 / 0.958 (fresh store) | 0.953 / 0.967 | — | |
| LME-S context tok/q | 28,869 | 2,367 (8.2%) | — | |
| LoCoMo QA (1,540) | 67.7% (strict 66.0) | **81.4%** (strict 81.3) | 74.5% (strict 73.9) | C vs A: 117 vs 328, χ²=99; C vs H: 137 vs 243, χ²=29 |
| LoCoMo R@5 / R@10 (session) | 0.511 / 0.695 | 0.820 / 0.877 | — | |
| LoCoMo context tok/q | 14,678 | 2,918 (19.9%) | 22,253 | |
| LoCoMo retrieve p50 / p95 ms | 17 / 29 | 10 / 60 | 47 / 72 | 272 sessions |
| Fallback-model rows | 34 (LoCoMo) | 3 LoCoMo, 1 LME | 13 LoCoMo | |
| PMB R@5 / R@10 (retrieval, k=20) | 0.924 / 0.941 (baseline) | 0.951 / **0.970** | — | p95 2.8 ms; PMB docs are too short to split |
| Ingest | LoCoMo 3 s | LoCoMo 69 s; LME-S 12,889 s (3.6 h) | LoCoMo 34 s | |

Per-type QA for LME-S, C versus the baseline's rrf/legacy runs: temporal
56.4 (was 36.8 / 38.3); knowledge-update 71.8 (64.1 / 60.3);
single-session-user 75.7 (77.1 / 78.6); single-session-assistant 87.5
(96.4 / 92.9); preference 36.7 (23.3 / 26.7); **multi-session 39.1 (57.9 /
49.6)**. LoCoMo per category, A → C: temporal 45.2 → 76.3, open-domain
76.7 → 90.1, single-hop 68.4 → 67.4, multi-hop 61.5 → 62.5.
H: 42.1, 87.0, 75.9, 68.8.

**Latency (the blocking finding):**

- LME-S C as run, 500 questions: p50 4.0 s, p95 35.5 s. The first 163
  questions ran with the product's 5 s fold retention (p50 11.9 s, p95
  42.3 s). The rest ran with `FOLD_RETENTION_S=3600` (p50 2.6 s, p95 6.1 s).
  Cause: `NativeMemoryStore` drops its fold and the incremental base after
  5 s without a rebuild. One unit's ingest takes about 25 s of writes, so the
  next search re-folds the entire log, O(store). Even with retention, latency
  still climbed from 0.15 to 5 s as the store grew, because the
  post-write extension also scales with the log.
- No-LLM replay on C's final store (1.0 GB, 337 wings, default retention,
  nothing else of ours running; the host had load ~8 from unrelated work):

  | Scenario | Retrieve latency |
  |---|---|
  | Open store | 10.7 s |
  | First search (cold fold) | **428 s** |
  | Warm fold, first touch of each wing (per-wing index build) | p50 180 ms, p95 3.05 s |
  | Warm, wing indexes built | **p50 6 ms, p95 7 ms** |
  | After a 6 s idle, 5 calls | **384–460 s each** |

  The ranking itself meets the budget. Keeping the folded state alive does
  not, and in a real session searches are usually more than 5 s apart.
- Ingest: passage writes are unbatched. Profiling showed 2,356 kernel appends
  at ~3.7 ms each for 20 documents, which dominates C's ~25 s per question.
  Embedding accounts for only ~2 s of that.

**D13 bar (A = legacy proxy for v2.0.1; C = winner):**

- (a) LME-S QA ≥ A + 5: **unmet**. 59.2 vs 55.8, +3.4, p≈0.23.
- (b) LoCoMo QA ≥ A + 5: **met**. 81.4 vs 67.7, +13.7 (strict +15.3),
  χ²=99.
- (c) QA ≥ the like-for-like hybrid baseline: **met on LoCoMo** (81.4 vs
  74.5, χ²=29). **Unmet or inconclusive on LME-S**: the subset gave 57 vs
  61, with 14 vs 18 discordant, so it is not significant. H's full LME-S was
  not run (~2.5 h).
- (d) PMB R@10 ≥ 0.941: **met**. 0.970, the same as D26: PMB docs are below
  the passage threshold.
- (e) Context tokens ≤ 50% of A: **met**. 8.2% on LME-S and 19.9% on LoCoMo.
- (f) Retrieve p95 ≤ 300 ms at full store with zero LLM calls: **unmet**.
  The zero-LLM part is met: the embedder is local and rerank is off. The p95
  part fails: as run, p95 was 35.5 s; after any idle over 5 s, a call takes
  6–8 minutes; first touch of a wing has p95 3.05 s. It is met only
  warm-steady (7 ms).

**Caveats:**

- L2 facts and the L3 briefing are still not exercised; AMB never runs the
  distiller. This measures L1 only.
- The LME-S A number is reused from the 2026-09-28 run (`a6a74ef`), which ran
  concurrently with another job and on a wiped/shared store. Its subset
  reproduction is within noise, but it is not a same-day run.
- The splitter fix and the adapter's embed-on-write change mean the P6 build
  as committed (`8214191`) would **not** have produced C's numbers. On this
  data, passages were inert until the fix.
- Multi-session QA fell sharply under passages (LME-S 57.9 → 39.1 against
  the baseline's rrf). k=10 passage hits, capped at 2 per drawer, cover fewer
  sessions than 10 whole sessions, which hurts "how many times across
  sessions" questions. Temporal and knowledge-update gained. LME-S's net gain
  is the difference between these.
- Only the full runs used timeouts and the fallback model. A had the most
  fallback rows, which, if anything, favours A.
- H ran as AMB ships it apart from the device and serialization changes. Its
  larger k (50 chunks) and ~23k-token context are part of its design, not a
  confound we introduced.
- Latency numbers were taken on a shared, loaded laptop (load avg 7–10 from
  unrelated work).

**Conclusion:** passages plus dates are the lever that paid off. Tokens fell
to 8–20% of A, LoCoMo QA rose +13.7 and beat AMB's hybrid baseline, and
temporal QA roughly doubled on both suites. Rerank and bge-small did not pay
and should stay off or at their defaults. D13 is still unmet on LME-S QA (+3.4)
and badly unmet on latency at scale. The latency failure comes from the 5 s
fold retention and from the O(log) cost of rebuilding after idle or writes,
not from ranking. Next, in order:
(1) make the folded state durable: persistent per-wing indexes kept current
from the kernel's subscribe stream, with no drop on idle and no O(log)
extension; (2) batch passage writes into one kernel batch per drawer;
(3) recover multi-session QA on the subset only, for example with a token
budget instead of a hit count (≈20 passages is still ≤50% of A) or a higher
per-drawer cap; (4) run H on full LME-S to settle (c).


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
