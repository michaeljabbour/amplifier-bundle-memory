"""Tests for amplifier_module_tool_memory.rerank (T6.4, D29).

Covers ``get_reranker``'s config-driven off/unknown/on paths, the
process-wide singleton cache, lazy load + loud-but-graceful unavailable
degradation (KG-N3's rerank half), truncation, and score ordering -- all via
monkeypatched fakes so the suite runs fast and without network access. ONE
real fastembed round-trip test is included, skipped cleanly when
``fastembed.rerank`` or a network-dependent first-run model download is not
available (matches the posture of ``test_embedder.py``'s real round-trip
test and this task's offline-CI requirement).
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from amplifier_module_tool_memory.rerank import (
    DEFAULT_RERANK_MAX_CHARS,
    DEFAULT_RERANK_MODEL,
    CrossEncoderReranker,
    get_reranker,
)


def _install_fake_cross_encoder(monkeypatch: pytest.MonkeyPatch, rerank_impl):
    """Install a fake ``fastembed.rerank.cross_encoder`` module.

    ``rerank_impl(query, documents) -> Iterable[float]`` backs
    ``TextCrossEncoder.rerank``; construction records call count via a
    mutable dict returned to the caller.
    """
    calls = {"constructed": 0}

    class _FakeTextCrossEncoder:
        def __init__(self, model_name: str) -> None:
            calls["constructed"] += 1
            self.model_name = model_name

        def rerank(self, query, documents):
            return rerank_impl(query, list(documents))

    fastembed_pkg = types.ModuleType("fastembed")
    rerank_pkg = types.ModuleType("fastembed.rerank")
    cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder_mod.TextCrossEncoder = _FakeTextCrossEncoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed_pkg)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
    monkeypatch.setitem(
        sys.modules, "fastembed.rerank.cross_encoder", cross_encoder_mod
    )
    return calls


class TestGetRerankerConfigPaths:
    def test_default_config_is_off(self) -> None:
        assert get_reranker({}) is None

    def test_explicit_off_returns_none(self) -> None:
        assert get_reranker({"rerank": "off"}) is None

    def test_unknown_mode_degrades_to_none(self) -> None:
        """An unrecognized 'rerank' value degrades to no-rerank rather than
        raising -- same posture as the embedder's degrade-on-failure paths."""
        assert get_reranker({"rerank": "not-a-real-mode"}) is None

    def test_cross_encoder_mode_returns_reranker(self) -> None:
        r = get_reranker({"rerank": "cross-encoder"})
        assert r is not None
        assert r.model_id == DEFAULT_RERANK_MODEL

    def test_custom_model_and_max_chars_honored(self) -> None:
        r = get_reranker(
            {
                "rerank": "cross-encoder",
                "rerank_model": "some/other-model",
                "rerank_max_chars": 42,
            }
        )
        assert r is not None
        assert r.model_id == "some/other-model"
        assert r.max_chars == 42  # type: ignore[attr-defined]


class TestSingletonCache:
    def test_same_config_returns_same_instance(self) -> None:
        r1 = get_reranker({"rerank": "cross-encoder"})
        r2 = get_reranker({"rerank": "cross-encoder"})
        assert r1 is r2

    def test_different_model_returns_different_instance(self) -> None:
        r1 = get_reranker({"rerank": "cross-encoder", "rerank_model": "model-a"})
        r2 = get_reranker({"rerank": "cross-encoder", "rerank_model": "model-b"})
        assert r1 is not r2

    def test_different_max_chars_returns_different_instance(self) -> None:
        r1 = get_reranker(
            {
                "rerank": "cross-encoder",
                "rerank_model": "model-c",
                "rerank_max_chars": 100,
            }
        )
        r2 = get_reranker(
            {
                "rerank": "cross-encoder",
                "rerank_model": "model-c",
                "rerank_max_chars": 200,
            }
        )
        assert r1 is not r2


class TestScoring:
    def test_empty_texts_returns_empty_without_loading_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_cross_encoder(
            monkeypatch, lambda q, docs: [0.0] * len(docs)
        )
        r = CrossEncoderReranker(model_name="test/empty-model")
        assert r.score("query", []) == []
        assert calls["constructed"] == 0  # lazy load never triggered

    def test_scores_preserve_input_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _rerank_impl(query, docs):
            # Score by position so we can assert order is preserved, not sorted.
            return [float(i) for i in range(len(docs))]

        _install_fake_cross_encoder(monkeypatch, _rerank_impl)
        r = CrossEncoderReranker(model_name="test/order-model")
        scores = r.score("query", ["first", "second", "third"])
        assert scores == [0.0, 1.0, 2.0]

    def test_scores_reflect_relevance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _rerank_impl(query, docs):
            return [10.0 if "relevant" in d else -5.0 for d in docs]

        _install_fake_cross_encoder(monkeypatch, _rerank_impl)
        r = CrossEncoderReranker(model_name="test/relevance-model")
        scores = r.score("q", ["this is relevant", "this is not"])
        assert scores[0] > scores[1]

    def test_truncates_text_before_scoring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_lengths: list[int] = []

        def _rerank_impl(query, docs):
            seen_lengths.extend(len(d) for d in docs)
            return [0.0] * len(docs)

        _install_fake_cross_encoder(monkeypatch, _rerank_impl)
        r = CrossEncoderReranker(model_name="test/truncate-model", max_chars=10)
        r.score("q", ["x" * 1000])
        assert seen_lengths == [10]

    def test_default_max_chars_matches_module_constant(self) -> None:
        r = CrossEncoderReranker()
        assert r.max_chars == DEFAULT_RERANK_MAX_CHARS

    def test_lazy_load_deferred_until_first_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_cross_encoder(
            monkeypatch, lambda q, docs: [0.0] * len(docs)
        )
        r = CrossEncoderReranker(model_name="test/lazy-model")
        assert calls["constructed"] == 0
        r.score("q", ["doc"])
        assert calls["constructed"] == 1

    def test_model_loaded_once_across_multiple_score_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_cross_encoder(
            monkeypatch, lambda q, docs: [0.0] * len(docs)
        )
        r = CrossEncoderReranker(model_name="test/once-model")
        r.score("q", ["doc1"])
        r.score("q", ["doc2"])
        r.score("q", ["doc3"])
        assert calls["constructed"] == 1


class TestUnavailableDegradation:
    def test_missing_fastembed_rerank_degrades_to_neutral_scores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KG-N3 (rerank half): if ``fastembed.rerank.cross_encoder`` cannot
        be imported at all, score() never raises -- it returns neutral
        (0.0) scores so the caller's fallback is 'keep the fused order'."""
        monkeypatch.delitem(
            sys.modules, "fastembed.rerank.cross_encoder", raising=False
        )
        monkeypatch.delitem(sys.modules, "fastembed.rerank", raising=False)
        monkeypatch.delitem(sys.modules, "fastembed", raising=False)
        # Force the import inside _ensure_loaded to fail deterministically.
        import builtins

        real_import = builtins.__import__

        def _boom_import(name, *args, **kwargs):
            if name == "fastembed.rerank.cross_encoder":
                raise ModuleNotFoundError("no fastembed.rerank extra installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom_import)

        r = CrossEncoderReranker(model_name="test/missing-extra")
        scores = r.score("q", ["a", "b", "c"])
        assert scores == [0.0, 0.0, 0.0]

    def test_forced_construction_failure_degrades_to_neutral_scores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BoomTextCrossEncoder:
            def __init__(self, model_name: str) -> None:
                raise RuntimeError("no network: could not download model")

        fastembed_pkg = types.ModuleType("fastembed")
        rerank_pkg = types.ModuleType("fastembed.rerank")
        cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
        cross_encoder_mod.TextCrossEncoder = _BoomTextCrossEncoder  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fastembed_pkg)
        monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
        monkeypatch.setitem(
            sys.modules, "fastembed.rerank.cross_encoder", cross_encoder_mod
        )

        r = CrossEncoderReranker(model_name="test/boom-model")
        assert r.score("q", ["a", "b"]) == [0.0, 0.0]

    def test_unavailable_state_is_sticky_no_repeated_load_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        class _BoomTextCrossEncoder:
            def __init__(self, model_name: str) -> None:
                calls["n"] += 1
                raise RuntimeError("boom")

        fastembed_pkg = types.ModuleType("fastembed")
        rerank_pkg = types.ModuleType("fastembed.rerank")
        cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
        cross_encoder_mod.TextCrossEncoder = _BoomTextCrossEncoder  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fastembed_pkg)
        monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
        monkeypatch.setitem(
            sys.modules, "fastembed.rerank.cross_encoder", cross_encoder_mod
        )

        r = CrossEncoderReranker(model_name="test/sticky-model")
        r.score("q", ["a"])
        r.score("q", ["b"])
        r.score("q", ["c"])
        assert calls["n"] == 1  # only tried once, then stayed unavailable


class TestConcurrentLoad:
    def test_concurrent_score_calls_load_model_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        calls = {"n": 0}
        lock = threading.Lock()

        class _FakeTextCrossEncoder:
            def __init__(self, model_name: str) -> None:
                with lock:
                    calls["n"] += 1

            def rerank(self, query, documents):
                return [0.0] * len(list(documents))

        fastembed_pkg = types.ModuleType("fastembed")
        rerank_pkg = types.ModuleType("fastembed.rerank")
        cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
        cross_encoder_mod.TextCrossEncoder = _FakeTextCrossEncoder  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fastembed_pkg)
        monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
        monkeypatch.setitem(
            sys.modules, "fastembed.rerank.cross_encoder", cross_encoder_mod
        )

        r = CrossEncoderReranker(model_name="test/concurrent-model")
        threads = [
            threading.Thread(target=r.score, args=("q", ["a"])) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls["n"] == 1


@pytest.mark.skipif(
    os.environ.get("AMPLIFIER_MEMORY_OFFLINE") == "1",
    reason="AMPLIFIER_MEMORY_OFFLINE=1: skipping real-model round trip",
)
def test_real_fastembed_cross_encoder_round_trip() -> None:
    """ONE real round-trip against the actual fastembed cross-encoder.

    Skipped cleanly (not failed) if ``fastembed.rerank`` is not installed or
    the first-run model download cannot complete offline -- mirrors
    ``test_embedder.py``'s real round-trip test.
    """
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
    except ImportError:
        pytest.skip("fastembed.rerank not installed")

    r = CrossEncoderReranker()
    try:
        scores = r.score(
            "capital of France",
            ["Paris is the capital of France.", "Bananas are yellow."],
        )
    except Exception as exc:  # noqa: BLE001 - any load/runtime failure -> skip
        pytest.skip(f"real cross-encoder unavailable: {exc}")

    if scores == [0.0, 0.0]:
        pytest.skip(
            "cross-encoder model unavailable offline (degraded to neutral scores)"
        )

    assert len(scores) == 2
    assert scores[0] > scores[1]
