# Experiment Journal

## 2026-06-07 — ANN + embedder spike: can the vector index move ChromaDB → amplifier-data?

**Hypothesis:** amplifier-data's vector lens can replace ChromaDB as memory's
semantic index without regressing retrieval quality (MemPalace reports 96.6%
R@5 on LongMemEval).

**Method (honest proxy — NOT full LongMemEval):** I do not have the LongMemEval
dataset/harness, so I measured retrieval-equivalence on a REAL corpus instead.
- Corpus: 143 real drawers mined by mempalace from this bundle's docs.
- Embedder: mempalace's own EmbeddingGemma ONNX (`mempalace.embedding`), 384-dim
  — the SAME embeddings fed to both indexes (apples-to-apples).
- Index A: amplifier-data `add_embedding` + `query_vector` (exact brute-force cosine).
- Index B: ChromaDB HNSW (cosine) — the incumbent.
- Quality: self-retrieval recall@5 + top-5 agreement over 40 queries.
- Speed: p50 query latency vs corpus size, padded with random 384-d vectors.
- Env: `/tmp/mpv` venv (mempalace+chromadb) + amplifier-data via sys.path
  (RUST_AVAILABLE=True). Scripts: `/tmp/spike.py`, `/tmp/spike_speed.py`.

**Results — QUALITY (zero regression):**
```
self-recall@5  amplifier-data(exact): 40/40 = 100.00%
self-recall@5  chromadb(HNSW)       : 40/40 = 100.00%
top-5 agreement (avg overlap)       : 100.00%
```
amplifier-data's exact cosine reproduces ChromaDB's HNSW results EXACTLY on
identical embeddings. The index backend itself does not cost any recall.

**Results — SPEED (the real gap, O(N) vs flat):**
```
      N   amplifier-data p50(ms)   chromadb p50(ms)
    143                     5.28               0.29
   1000                    39.81               0.50
   5000                   204.19               0.77
  10000                   447.66               0.94
  20000                   893.38               0.93
```
amplifier-data brute-force is linear (~0.045 ms/vector); ChromaDB HNSW stays
sub-millisecond regardless of N.

**Conclusion / decision:** The index CAN move on QUALITY (zero regression) but
CANNOT move on PERFORMANCE until amplifier-data gains an ANN lens (HNSW/IVF).
The interject hook fires multiple times per turn and a long-lived palace reaches
10k+ drawers, where 448 ms/query brute-force is unusable on the hot path.
→ **Keep ChromaDB as the semantic index; use amplifier-data for the verbatim +
KG + scope floor (the dual-write architecture already built).** Revisit a full
index move only when amplifier-data ships an ANN lens. R@5 is not threatened by
the backend choice — latency is.

**Caveat:** This is a 143-drawer equivalence proxy, not a LongMemEval R@5
reproduction (needs the dataset + harness). The quality result is content-
limited; the speed result is definitive (content-agnostic, O(N) is structural).
