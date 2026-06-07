"""T1-MEM-4 — mutable embeddings re-embed seam (OFF BY DEFAULT, highest risk).

Today MemPalace embeddings are write-once. This module adds a *seam* for moving
embeddings via a bounded moving-average update with pinned anchors. It is the
lowest-priority, highest-risk primitive in the constellation, so it ships:

* **off by default** — ``MutableEmbeddingConfig.enabled`` is ``False``; with it
  off (or alpha=0, or a pinned vector) ``reembed`` is the identity, so merely
  importing this module changes nothing.
* **NOT wired into the live vector path** — enabling it in production is gated
  on a recall/ablation benchmark proving no silent degradation (see the module
  ``benchmark`` helper and ``tests/``). We do not flip it on here.
* **with no cognitive claim** — this is a numerical update rule, nothing more.

Pure functions only: no I/O, no mutation of inputs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "MutableEmbeddingConfig",
    "l2_normalize",
    "cosine_distance",
    "reembed",
    "recall_overlap",
]


@dataclass(frozen=True)
class MutableEmbeddingConfig:
    """Controls the re-embed seam. Defaults are a hard OFF switch.

    enabled     : master switch. False -> reembed is the identity.
    alpha       : moving-average rate in [0,1]. 0 -> identity; 1 -> jump to target.
    renormalize : keep the result on the unit sphere (recommended for cosine).
    """

    enabled: bool = False
    alpha: float = 0.1
    renormalize: bool = True


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Return the unit-norm version of ``vec`` (unchanged if zero-norm)."""
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0.0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """1 - cosine similarity, in [0, 2]. Used to measure embedding drift."""
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(x) * float(x) for x in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    return round(1.0 - dot / (na * nb), 10)


def reembed(
    current: Sequence[float],
    target: Sequence[float],
    *,
    config: MutableEmbeddingConfig = MutableEmbeddingConfig(),
    pinned: bool = False,
) -> list[float]:
    """Moving-average update of an embedding toward ``target``.

        new = (1 - alpha) * current + alpha * target

    Returns ``current`` unchanged (the identity) when the seam is disabled,
    ``alpha`` is 0, the vector is a pinned anchor, or shapes mismatch — i.e. it
    is impossible to silently move an embedding without explicitly opting in.
    """
    cur = [float(x) for x in current]
    if not config.enabled or pinned or config.alpha <= 0.0:
        return cur
    if len(cur) != len(target):
        return cur
    a = min(1.0, max(0.0, float(config.alpha)))
    blended = [(1.0 - a) * c + a * float(t) for c, t in zip(cur, target)]
    if config.renormalize:
        blended = l2_normalize(blended)
    return blended


def recall_overlap(before: Sequence[str], after: Sequence[str], k: int) -> float:
    """Fraction of the top-``k`` ``before`` ids still present in top-``k`` ``after``.

    A benchmark primitive: a re-embed pass is only safe to enable if this stays
    at/above the recall floor across a representative query set. 1.0 = no change.
    """
    if k <= 0:
        return 1.0
    b = list(before)[:k]
    a = set(list(after)[:k])
    if not b:
        return 1.0
    kept = sum(1 for x in b if x in a)
    return kept / len(b)
