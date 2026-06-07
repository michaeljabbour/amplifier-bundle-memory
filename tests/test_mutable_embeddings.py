"""T1-MEM-4 — mutable-embedding re-embed seam (off-by-default, benchmark-gated).

These tests prove the seam is a HARD no-op unless explicitly enabled, that
pinned anchors never move, and that the update rule is bounded and well-behaved.
The seam is NOT wired into the live vector path; enabling it in production is
gated on recall_overlap staying at/above the recall floor.
"""

from __future__ import annotations

from amplifier_module_tool_mempalace.embeddings import (
    MutableEmbeddingConfig,
    cosine_distance,
    l2_normalize,
    recall_overlap,
    reembed,
)


def test_disabled_by_default_is_identity() -> None:
    cur = [1.0, 0.0, 0.0]
    out = reembed(cur, [0.0, 1.0, 0.0])  # default config: enabled=False
    assert out == cur


def test_pinned_anchor_never_moves() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=0.5, renormalize=False)
    cur = [1.0, 0.0]
    out = reembed(cur, [0.0, 1.0], config=cfg, pinned=True)
    assert out == cur


def test_alpha_zero_is_identity_even_when_enabled() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=0.0)
    cur = [1.0, 0.0]
    assert reembed(cur, [0.0, 1.0], config=cfg) == cur


def test_enabled_moves_toward_target_bounded() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=0.5, renormalize=False)
    out = reembed([0.0, 0.0], [1.0, 1.0], config=cfg)
    assert out == [0.5, 0.5]  # exactly the moving average


def test_alpha_one_jumps_to_target_direction() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=1.0, renormalize=True)
    out = reembed([1.0, 0.0], [0.0, 5.0], config=cfg)
    assert out == l2_normalize([0.0, 5.0]) == [0.0, 1.0]


def test_shape_mismatch_is_safe_identity() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=0.5)
    cur = [1.0, 0.0, 0.0]
    assert reembed(cur, [1.0, 0.0], config=cfg) == cur


def test_renormalize_keeps_unit_norm() -> None:
    cfg = MutableEmbeddingConfig(enabled=True, alpha=0.3, renormalize=True)
    out = reembed([1.0, 0.0], [0.0, 1.0], config=cfg)
    norm = sum(x * x for x in out) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_drift_is_monotonic_in_alpha() -> None:
    cur = [1.0, 0.0]
    tgt = [0.0, 1.0]
    d_small = cosine_distance(
        cur, reembed(cur, tgt, config=MutableEmbeddingConfig(enabled=True, alpha=0.1))
    )
    d_big = cosine_distance(
        cur, reembed(cur, tgt, config=MutableEmbeddingConfig(enabled=True, alpha=0.9))
    )
    assert d_small < d_big


def test_recall_overlap_benchmark_primitive() -> None:
    before = ["a", "b", "c", "d", "e"]
    assert recall_overlap(before, before, k=5) == 1.0
    # one of the top-5 dropped out -> 0.8 recall
    after = ["a", "b", "c", "d", "z"]
    assert abs(recall_overlap(before, after, k=5) - 0.8) < 1e-9
