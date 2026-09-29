"""Tests for FastEmbedEmbedder's model-choice surface (T6.5, D29).

Covers ``model_id``/``dim`` resolution, ``embed_query``/``embed_documents``
asymmetric-retrieval paths (with fallback to plain ``embed`` for models with
no query/passage-specific implementation), and backward compatibility of the
pre-existing ``embed`` alias -- all via monkeypatched fakes so the suite runs
fast and without network access. Real-model tests are skipped cleanly when
fastembed or the model download is not available offline (matches
``test_embedder.py``'s posture and this task's offline-CI requirement).
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from amplifier_module_tool_memory.embedder import (
    BGE_SMALL_MODEL,
    DEFAULT_MODEL,
    EmbedderUnavailable,
    FastEmbedEmbedder,
)


def _install_fake_fastembed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_query_embed: bool = False,
    with_passage_embed: bool = False,
):
    """Install a fake ``fastembed`` module with a controllable ``TextEmbedding``.

    When *with_query_embed*/*with_passage_embed* are False, the fake model
    has no such attribute at all (``getattr(..., None)`` returns ``None``),
    exercising the fallback-to-plain-``embed`` path.
    """

    class _FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def embed(self, texts):
            for t in texts:
                yield [float(len(t)), 0.0, 0.0]

    if with_query_embed:

        def _query_embed(self, texts):
            for t in texts:
                yield [999.0, 0.0, 0.0]  # obviously distinct from embed()

        _FakeTextEmbedding.query_embed = _query_embed  # type: ignore[attr-defined]

    if with_passage_embed:

        def _passage_embed(self, texts):
            for t in texts:
                yield [0.0, 999.0, 0.0]  # obviously distinct from embed()

        _FakeTextEmbedding.passage_embed = _passage_embed  # type: ignore[attr-defined]

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)


class TestModelIdentity:
    def test_default_model_dim_is_384(self) -> None:
        e = FastEmbedEmbedder()
        assert e.dim == 384
        assert e.model_id == f"{DEFAULT_MODEL}:384"

    def test_bge_small_model_dim_is_384(self) -> None:
        e = FastEmbedEmbedder(BGE_SMALL_MODEL)
        assert e.dim == 384
        assert e.model_id == f"{BGE_SMALL_MODEL}:384"

    def test_model_id_distinguishes_models_of_same_dim(self) -> None:
        """Both supported models are 384-dim, but model_id must still
        differ -- callers compare model_id, not dim, to detect a config
        change that requires a re-embed sweep."""
        a = FastEmbedEmbedder(DEFAULT_MODEL)
        b = FastEmbedEmbedder(BGE_SMALL_MODEL)
        assert a.dim == b.dim == 384
        assert a.model_id != b.model_id

    def test_unknown_model_falls_back_to_bare_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model name unknown to both the local map and fastembed's own
        registry degrades dim to None and model_id to the bare name --
        never raises at construction time."""
        _install_fake_fastembed(monkeypatch)

        class _NoSuchModelTextEmbedding:
            @staticmethod
            def get_embedding_size(model_name: str) -> int:
                raise ValueError(f"unknown model: {model_name}")

        sys.modules["fastembed"].TextEmbedding.get_embedding_size = (  # type: ignore[attr-defined]
            _NoSuchModelTextEmbedding.get_embedding_size
        )

        e = FastEmbedEmbedder("totally/made-up-model")
        assert e.dim is None
        assert e.model_id == "totally/made-up-model"

    def test_construction_never_loads_model_or_touches_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving dim/model_id at construction must not warm the model --
        matches the embedder's existing 'construction is cheap' contract."""
        calls = {"n": 0}

        class _FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                calls["n"] += 1

        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fastembed", fake_module)

        FastEmbedEmbedder(DEFAULT_MODEL)  # known model: never even imports fastembed
        assert calls["n"] == 0


class TestBackwardCompatibleEmbed:
    def test_embed_unchanged_for_default_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch)
        e = FastEmbedEmbedder()
        e.warm()
        assert e.embed("hello") == [5.0, 0.0, 0.0]

    def test_embed_before_warm_raises(self) -> None:
        e = FastEmbedEmbedder()
        with pytest.raises(EmbedderUnavailable):
            e.embed("hello")


class TestEmbedQuery:
    def test_uses_query_embed_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch, with_query_embed=True)
        e = FastEmbedEmbedder(BGE_SMALL_MODEL)
        e.warm()
        assert e.embed_query("what is x?") == [999.0, 0.0, 0.0]

    def test_falls_back_to_embed_when_query_embed_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch, with_query_embed=False)
        e = FastEmbedEmbedder()
        e.warm()
        assert e.embed_query("hello") == e.embed("hello")

    def test_embed_query_before_warm_raises(self) -> None:
        e = FastEmbedEmbedder()
        with pytest.raises(EmbedderUnavailable):
            e.embed_query("hello")


class TestEmbedDocuments:
    def test_uses_passage_embed_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch, with_passage_embed=True)
        e = FastEmbedEmbedder(BGE_SMALL_MODEL)
        e.warm()
        assert e.embed_documents(["a", "b"]) == [
            [0.0, 999.0, 0.0],
            [0.0, 999.0, 0.0],
        ]

    def test_falls_back_to_embed_per_text_when_passage_embed_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch, with_passage_embed=False)
        e = FastEmbedEmbedder()
        e.warm()
        assert e.embed_documents(["ab", "abc"]) == [e.embed("ab"), e.embed("abc")]

    def test_empty_list_returns_empty_without_touching_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_fastembed(monkeypatch)
        e = FastEmbedEmbedder()
        e.warm()
        assert e.embed_documents([]) == []

    def test_embed_documents_before_warm_raises(self) -> None:
        e = FastEmbedEmbedder()
        with pytest.raises(EmbedderUnavailable):
            e.embed_documents(["hello"])

    def test_preserves_input_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_fastembed(monkeypatch)
        e = FastEmbedEmbedder()
        e.warm()
        # embed()'s fake scores by len(text) -- verify order isn't shuffled.
        vecs = e.embed_documents(["a", "abc", "ab"])
        assert [v[0] for v in vecs] == [1.0, 3.0, 2.0]


@pytest.mark.skipif(
    os.environ.get("AMPLIFIER_MEMORY_OFFLINE") == "1",
    reason="AMPLIFIER_MEMORY_OFFLINE=1: skipping real-model round trip",
)
class TestRealModelRoundTrip:
    """Real fastembed round trips for both supported models. Skipped
    cleanly (not failed) if fastembed is not installed or a model's
    first-run download cannot complete offline."""

    def _warm_or_skip(self, model_name: str) -> FastEmbedEmbedder:
        try:
            import fastembed  # noqa: F401
        except ImportError:
            pytest.skip("fastembed not installed")
        e = FastEmbedEmbedder(model_name)
        e.warm()
        if not e.ready:
            pytest.skip(f"model {model_name!r} unavailable offline: {e.failed}")
        return e

    def test_default_model_real_round_trip(self) -> None:
        e = self._warm_or_skip(DEFAULT_MODEL)
        vec = e.embed("hello world")
        assert len(vec) == 384
        assert e.dim == 384

    def test_bge_small_model_real_round_trip(self) -> None:
        e = self._warm_or_skip(BGE_SMALL_MODEL)
        vec = e.embed_query("hello world")
        assert len(vec) == 384
        docs = e.embed_documents(["doc one", "doc two"])
        assert len(docs) == 2
        assert all(len(d) == 384 for d in docs)
