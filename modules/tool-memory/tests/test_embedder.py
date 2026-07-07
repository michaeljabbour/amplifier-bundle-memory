"""Tests for amplifier_module_tool_memory.embedder (\u00a74 of the native-cutover design).

Covers lifecycle (warm/ready/failed), the loud-but-graceful forced-fail path
(KG-N3's embedder half), and the deterministic lexical_score term (\u00a76.1/\u00a76.2).
ONE real fastembed round-trip test is included (skipped when fastembed is not
installed) -- everything else uses monkeypatching so the suite runs fast and
without network access.
"""

from __future__ import annotations

import threading

import pytest
from amplifier_module_tool_memory.embedder import (
    DEFAULT_MODEL,
    EmbedderUnavailable,
    FastEmbedEmbedder,
    lexical_score,
)


class TestLifecycle:
    def test_not_ready_before_warm(self) -> None:
        e = FastEmbedEmbedder()
        assert e.ready is False
        assert e.failed is None

    def test_embed_before_warm_raises(self) -> None:
        e = FastEmbedEmbedder()
        with pytest.raises(EmbedderUnavailable):
            e.embed("hello")

    def test_warm_success_sets_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeModel:
            def embed(self, texts):  # noqa: ANN001
                for _ in texts:
                    yield [0.1, 0.2, 0.3]

        class _FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                self.model_name = model_name

            def embed(self, texts):  # noqa: ANN001
                for _ in texts:
                    yield [0.1, 0.2, 0.3]

        import sys
        import types

        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)

        e = FastEmbedEmbedder()
        e.warm()
        assert e.ready is True
        assert e.failed is None
        vec = e.embed("hello")
        assert vec == [0.1, 0.2, 0.3]

    def test_warm_failure_is_loud_but_graceful(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KG-N3 (embedder half): a forced warm-load failure never raises out of
        warm(), sets .failed with a reason, and embed() raises a typed error
        rather than crashing the caller."""
        import sys
        import types

        fake_module = types.ModuleType("fastembed")

        class _BoomTextEmbedding:
            def __init__(self, model_name: str) -> None:
                raise RuntimeError("no network: could not download model")

        fake_module.TextEmbedding = _BoomTextEmbedding  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)

        e = FastEmbedEmbedder()
        e.warm()  # must not raise
        assert e.ready is False
        assert e.failed is not None
        assert "no network" in e.failed

        with pytest.raises(EmbedderUnavailable):
            e.embed("hello")

    def test_warm_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        import sys
        import types

        class _FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                calls["n"] += 1

            def embed(self, texts):  # noqa: ANN001
                for _ in texts:
                    yield [1.0]

        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)

        e = FastEmbedEmbedder()
        e.warm()
        e.warm()
        e.warm()
        assert calls["n"] == 1

    def test_warm_concurrent_from_multiple_threads_loads_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        lock = threading.Lock()

        import sys
        import types

        class _FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                with lock:
                    calls["n"] += 1

            def embed(self, texts):  # noqa: ANN001
                for _ in texts:
                    yield [1.0]

        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)

        e = FastEmbedEmbedder()
        threads = [threading.Thread(target=e.warm) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls["n"] == 1
        assert e.ready is True


class TestLexicalScore:
    def test_empty_query_or_text_is_zero(self) -> None:
        assert lexical_score("", "some text") == 0.0
        assert lexical_score("query", "") == 0.0
        assert lexical_score("", "") == 0.0

    def test_full_overlap_is_one(self) -> None:
        assert lexical_score("hello world", "hello world") == 1.0

    def test_partial_overlap(self) -> None:
        # 1 of 2 query tokens present -> 0.5
        assert lexical_score("hello galaxy", "hello world") == 0.5

    def test_no_overlap_is_zero(self) -> None:
        assert lexical_score("xylophone", "hello world") == 0.0

    def test_case_insensitive(self) -> None:
        assert lexical_score("HELLO", "hello world") == 1.0

    def test_deterministic(self) -> None:
        results = {
            lexical_score("auth decision", "we decided on auth") for _ in range(20)
        }
        assert len(results) == 1


class TestRealFastembedRoundTrip:
    """The one REAL (non-mocked) embedding round-trip, per the task's evidence
    requirement. Requires the fastembed package AND (on first run only) network
    access to download the model to the local HF cache. Skipped entirely when
    fastembed is not installed -- never a hard requirement for the rest of the
    suite (\u00a713 Definition of Done: "tests must not hard-require the model").
    """

    def test_real_embed_produces_384_dim_vector(self) -> None:
        pytest.importorskip("fastembed")
        e = FastEmbedEmbedder(DEFAULT_MODEL)
        e.warm()
        if not e.ready:
            pytest.skip(
                f"fastembed model warm-load failed (likely no network): {e.failed}"
            )
        vec = e.embed("We decided to keep the manifest verbatim.")
        assert len(vec) == 384
        assert all(isinstance(x, float) for x in vec)
        # Same text -> same vector (deterministic inference).
        vec2 = e.embed("We decided to keep the manifest verbatim.")
        assert vec == vec2
