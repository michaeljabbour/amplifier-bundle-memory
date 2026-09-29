"""Offline smoke test for :mod:`amplifier_memory_provider`.

Does NOT require AMB (``memory_bench``) to be installed: if it is not
importable, a minimal stand-in ``memory_bench.models``/``memory_bench.
memory.base`` is monkeypatched into ``sys.modules`` first, carrying just
enough of AMB's real, verified shapes (``Document`` dataclass fields;
``MemoryProvider`` ABC's optional/abstract methods) for the adapter to
import and run against. No AMB code is copied here -- these are
independent re-declarations of the same field/method NAMES, written from
this repo's own reading of AMB's source (see amplifier_memory_provider.py's
module docstring for the exact interface verified).

Runs with the shared dev venv used for tool-memory's own suite (has
amplifier-data with ``RUST_AVAILABLE=True``, and amplifier-core):
    ~/dev/.venv/bin/python -m pytest benchmarks/amb/tests -q

Forces ``embedder="none"`` (lexical-only) so the test never needs network
access to download an embedding model.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Make the adapter importable without installing it as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_fake_memory_bench() -> None:
    """Monkeypatch a minimal ``memory_bench`` into ``sys.modules`` if the
    real AMB package is not installed in this interpreter."""
    if "memory_bench" in sys.modules:
        return
    try:
        import memory_bench  # noqa: F401

        return
    except ImportError:
        pass

    from abc import ABC, abstractmethod
    from dataclasses import dataclass, field

    pkg = types.ModuleType("memory_bench")

    models_mod = types.ModuleType("memory_bench.models")

    @dataclass
    class Document:
        id: str
        content: str
        user_id: str | None = None
        messages: list[dict] | None = None
        timestamp: str | None = None
        context: str | None = None
        source_ids: list[str] | None = None
        tags: list[str] | None = None

    models_mod.Document = Document  # type: ignore[attr-defined]

    memory_pkg = types.ModuleType("memory_bench.memory")
    base_mod = types.ModuleType("memory_bench.memory.base")

    class MemoryProvider(ABC):
        name: str
        description: str
        kind: str
        concurrency: int = 4
        supports_filters: bool = False

        def initialize(self) -> None: ...

        def cleanup(self) -> None: ...

        def prepare(
            self, store_dir: Path, unit_ids: set[str] | None = None, reset: bool = True
        ) -> None: ...

        @abstractmethod
        def ingest(self, documents: list) -> None: ...

        @abstractmethod
        def retrieve(
            self,
            query: str,
            k: int = 10,
            user_id: str | None = None,
            query_timestamp: str | None = None,
            filters: dict | None = None,
        ) -> tuple[list, dict | None]: ...

    base_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]

    memory_pkg.base = base_mod  # type: ignore[attr-defined]
    memory_pkg.REGISTRY = {}  # type: ignore[attr-defined]
    pkg.models = models_mod  # type: ignore[attr-defined]
    pkg.memory = memory_pkg  # type: ignore[attr-defined]

    sys.modules["memory_bench"] = pkg
    sys.modules["memory_bench.models"] = models_mod
    sys.modules["memory_bench.memory"] = memory_pkg
    sys.modules["memory_bench.memory.base"] = base_mod
    del field  # imported only for parity with AMB's real dataclass; unused here


_install_fake_memory_bench()

import pytest
from amplifier_memory_provider import AmplifierMemoryProvider

# Both the real and fake memory_bench.models expose Document the same way.
from memory_bench.models import Document

pytest.importorskip("amplifier_module_tool_memory.store")
pytest.importorskip("amplifier_data")


@pytest.fixture
def provider(tmp_path: Path):
    p = AmplifierMemoryProvider(home=tmp_path / "amb_store", embedder="none")
    yield p
    p.cleanup()


def _docs() -> list[Document]:
    return [
        Document(
            id="d1",
            content="Alice's favorite color is blue.",
            user_id="alice",
            tags=["preferences"],
        ),
        Document(
            id="d2",
            content="Alice works as a marine biologist.",
            user_id="alice",
            tags=["facts"],
        ),
        Document(
            id="d3",
            content="The error code ERR_4417 means disk full.",
            user_id="alice",
            tags=["facts"],
        ),
        Document(
            id="d4",
            content="Bob's favorite color is green.",
            user_id="bob",
            tags=["preferences"],
        ),
        Document(
            id="d5", content="Bob works as a chef.", user_id="bob", tags=["facts"]
        ),
    ]


def test_ingest_and_retrieve_exact_identifier(
    provider: AmplifierMemoryProvider,
) -> None:
    """Filing 5 docs across 2 users, an exact-identifier query for alice
    recovers the doc carrying that identifier with its ORIGINAL AMB id."""
    provider.ingest(_docs())

    docs, raw = provider.retrieve("ERR_4417", k=5, user_id="alice")

    assert docs, "expected at least one hit for an exact-identifier query"
    assert docs[0].id == "d3"
    assert "ERR_4417" in docs[0].content
    # raw_response must stay None: AMB prompts replace the rendered context
    # with json.dumps(raw_response) when it is non-empty.
    assert raw is None
    assert provider.last_raw is not None
    assert provider.last_raw["scores"][docs[0].id] > 0.0


def test_user_isolation(provider: AmplifierMemoryProvider) -> None:
    """A query scoped to bob never returns alice's documents, and vice versa."""
    provider.ingest(_docs())

    alice_docs, _ = provider.retrieve("favorite color", k=5, user_id="alice")
    bob_docs, _ = provider.retrieve("favorite color", k=5, user_id="bob")

    alice_ids = {d.id for d in alice_docs}
    bob_ids = {d.id for d in bob_docs}

    assert alice_ids, "alice should have at least one hit for 'favorite color'"
    assert bob_ids, "bob should have at least one hit for 'favorite color'"
    assert alice_ids.isdisjoint(bob_ids)
    assert "d1" in alice_ids  # Alice's own favorite-color doc
    assert "d4" in bob_ids  # Bob's own favorite-color doc
    assert "d4" not in alice_ids
    assert "d1" not in bob_ids


def test_embedder_none_never_touches_a_model(provider: AmplifierMemoryProvider) -> None:
    """embedder='none' must never attempt to load fastembed -- this is what
    keeps the smoke test runnable fully offline."""
    provider.ingest(_docs())
    docs, raw = provider.retrieve("chef", k=5, user_id="bob")

    assert provider.last_raw is not None
    assert provider.last_raw["degraded"] is True  # no query vector computed
    assert provider._embedder is None
    assert provider._embedder_load_attempted is False
    assert any(d.id == "d5" for d in docs)


@pytest.mark.parametrize("fusion", ["rrf", "legacy"])
def test_fusion_selectable(tmp_path: Path, fusion: str) -> None:
    """Both fusion modes run end-to-end and are reported in the raw payload."""
    p = AmplifierMemoryProvider(home=tmp_path / "s", embedder="none", fusion=fusion)
    try:
        p.ingest(_docs())
        docs, _ = p.retrieve("ERR_4417", k=5, user_id="alice")
        assert p.last_raw is not None and p.last_raw["fusion"] == fusion
        assert docs and docs[0].id == "d3"
    finally:
        p.cleanup()


def test_fusion_and_layers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMPLIFIER_AMB_FUSION", "legacy")
    monkeypatch.setenv("AMPLIFIER_AMB_LAYERS", "drawer,fact")
    p = AmplifierMemoryProvider(embedder="none")
    assert p._fusion == "legacy"
    assert p._layers == ("drawer", "fact")
    monkeypatch.setenv("AMPLIFIER_AMB_FUSION", "bogus")
    with pytest.raises(ValueError):
        AmplifierMemoryProvider(embedder="none")


def test_trace_env_records_ids(
    provider: AmplifierMemoryProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AMPLIFIER_AMB_TRACE", str(trace))
    provider.ingest(_docs())
    docs, _ = provider.retrieve("ERR_4417", k=5, user_id="alice")
    row = json.loads(trace.read_text().splitlines()[-1])
    assert row["user_id"] == "alice" and row["ids"] == [d.id for d in docs]
    assert row["granularity"] == provider._granularity
    assert row["rerank"] == provider._rerank


# ---------------------------------------------------------------------------
# T6.3: date-stamped, passage-aware evidence rendering.
#
# ``store.search`` at HEAD does not yet return granularity/rerank or
# passage-shaped hits (T6.1/T6.2 land separately) -- these tests substitute a
# tiny fake store (attached directly to ``provider._store``, bypassing
# ``_ensure_store()``) that speaks the NEW hit/param shape, so the adapter's
# own T6.3 logic (signature guard, doc-id mapping, dedup/concat, date stamp)
# is exercised deterministically without waiting on that substrate work.
# ---------------------------------------------------------------------------


class _FakeGranularityStore:
    """Speaks the post-T6.1/T6.2 ``search`` shape: accepts ``granularity``/
    ``rerank`` kwargs and can return passage-layer hits."""

    def __init__(self) -> None:
        self.filed: list[tuple[str, dict]] = []
        self.search_calls: list[dict] = []
        self._next_ref = 0
        self.hits_to_return: list[dict] = []

    def file(self, **kwargs):
        self._next_ref += 1
        ref = f"drawer{self._next_ref}"
        self.filed.append((ref, kwargs))
        return ref

    def search(
        self,
        query_vector,
        k,
        *,
        wing=None,
        room=None,
        lexical_query=None,
        fusion=None,
        layers=None,
        granularity=None,
        rerank=None,
    ):
        self.search_calls.append(
            {
                "wing": wing,
                "lexical_query": lexical_query,
                "fusion": fusion,
                "layers": layers,
                "granularity": granularity,
                "rerank": rerank,
            }
        )
        return list(self.hits_to_return)

    def close(self) -> None:
        pass


class _FakeOldStore:
    """Speaks the PRE-T6.1 ``search`` shape -- no granularity/rerank params
    at all. A signature guard must never pass those kwargs to this store."""

    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.hits_to_return: list[dict] = []

    def file(self, **kwargs):
        return "drawer1"

    def search(
        self,
        query_vector,
        k,
        *,
        wing=None,
        room=None,
        lexical_query=None,
        fusion=None,
        layers=None,
    ):
        self.search_calls.append(
            {"wing": wing, "lexical_query": lexical_query, "fusion": fusion}
        )
        return list(self.hits_to_return)

    def close(self) -> None:
        pass


def test_signature_guard_never_passes_unsupported_kwargs_to_old_store(
    tmp_path: Path,
) -> None:
    """An old-style store (no granularity/rerank params) must not blow up --
    the adapter probes ``inspect.signature`` before adding those kwargs."""
    provider = AmplifierMemoryProvider(home=tmp_path / "s", embedder="none")
    fake = _FakeOldStore()
    fake.hits_to_return = [
        {"ref": "d1", "source": "doc1", "content": "hello", "score": 0.5, "room": "r"}
    ]
    provider._store = fake

    docs, raw = provider.retrieve("hello", k=5, user_id="alice")
    del raw

    assert len(fake.search_calls) == 1
    assert "granularity" not in fake.search_calls[0]
    assert "rerank" not in fake.search_calls[0]
    assert docs and docs[0].id == "doc1"


def test_granularity_and_rerank_passed_through_when_store_supports_them(
    tmp_path: Path,
) -> None:
    provider = AmplifierMemoryProvider(
        home=tmp_path / "s", embedder="none", granularity="passage", rerank=True
    )
    fake = _FakeGranularityStore()
    fake.hits_to_return = [
        {"ref": "d1", "source": "doc1", "content": "hi", "score": 0.5, "room": "r"}
    ]
    provider._store = fake

    provider.retrieve("hi", k=5, user_id="alice")

    assert fake.search_calls[0]["granularity"] == "passage"
    assert fake.search_calls[0]["rerank"] is True


def test_passage_hits_map_back_to_original_doc_id(tmp_path: Path) -> None:
    """A passage hit's own ``ref`` is the passage cell -- retrieve() must
    resolve the ORIGINAL AMB doc id via ``drawer_ref``, populated by ingest()."""
    provider = AmplifierMemoryProvider(home=tmp_path / "s", embedder="none")
    fake = _FakeGranularityStore()
    provider._store = fake

    provider.ingest(
        [Document(id="original-doc", content="full drawer text", user_id="alice")]
    )
    drawer_ref = fake.filed[0][0]

    fake.hits_to_return = [
        {
            "ref": "passage-1",
            "drawer_ref": drawer_ref,
            "layer": "passage",
            "content": "passage snippet",
            "score": 0.7,
            "room": "r",
            "span": [0, 20],
        }
    ]
    docs, _ = provider.retrieve("query", k=5, user_id="alice")

    assert len(docs) == 1
    assert docs[0].id == "original-doc"
    assert "passage snippet" in docs[0].content


def test_multiple_passages_of_same_parent_dedup_and_concatenate_in_span_order(
    tmp_path: Path,
) -> None:
    provider = AmplifierMemoryProvider(
        home=tmp_path / "s", embedder="none", dates=False
    )
    fake = _FakeGranularityStore()
    provider._store = fake

    provider.ingest([Document(id="doc1", content="whole thing", user_id="alice")])
    drawer_ref = fake.filed[0][0]

    fake.hits_to_return = [
        # Deliberately out of span order -- output must be span-sorted.
        {
            "ref": "p2",
            "drawer_ref": drawer_ref,
            "layer": "passage",
            "content": "second half",
            "score": 0.6,
            "room": "r",
            "span": [50, 100],
        },
        {
            "ref": "p1",
            "drawer_ref": drawer_ref,
            "layer": "passage",
            "content": "first half",
            "score": 0.9,
            "room": "r",
            "span": [0, 50],
        },
    ]
    docs, _ = provider.retrieve("query", k=5, user_id="alice")

    assert len(docs) == 1  # deduped to ONE document, not two
    assert docs[0].id == "doc1"
    assert docs[0].content == "first half\n\u2026\nsecond half"


@pytest.mark.parametrize("dates_on", [True, False])
def test_date_stamp_toggle(tmp_path: Path, dates_on: bool) -> None:
    provider = AmplifierMemoryProvider(
        home=tmp_path / "s", embedder="none", dates=dates_on
    )
    fake = _FakeGranularityStore()
    fake.hits_to_return = [
        {
            "ref": "d1",
            "source": "doc1",
            "content": "some content",
            "score": 0.5,
            "room": "r",
            "filed_at": "2026-09-28T12:00:00+00:00",
        }
    ]
    provider._store = fake

    docs, _ = provider.retrieve("query", k=5, user_id="alice")

    assert len(docs) == 1
    if dates_on:
        assert docs[0].content == "[2026-09-28] some content"
    else:
        assert docs[0].content == "some content"


def test_date_stamp_prefers_observed_at_over_filed_at(tmp_path: Path) -> None:
    provider = AmplifierMemoryProvider(home=tmp_path / "s", embedder="none")
    fake = _FakeGranularityStore()
    fake.hits_to_return = [
        {
            "ref": "d1",
            "source": "doc1",
            "content": "x",
            "score": 0.5,
            "room": "r",
            "filed_at": "2026-01-01T00:00:00+00:00",
            "observed_at": "2020-05-05T00:00:00+00:00",
        }
    ]
    provider._store = fake

    docs, _ = provider.retrieve("query", k=5, user_id="alice")
    assert docs[0].content.startswith("[2020-05-05]")


def test_date_stamp_missing_produces_no_prefix(tmp_path: Path) -> None:
    provider = AmplifierMemoryProvider(home=tmp_path / "s", embedder="none")
    fake = _FakeGranularityStore()
    fake.hits_to_return = [
        {"ref": "d1", "source": "doc1", "content": "plain", "score": 0.5, "room": "r"}
    ]
    provider._store = fake

    docs, _ = provider.retrieve("query", k=5, user_id="alice")
    assert docs[0].content == "plain"


def test_granularity_rerank_dates_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMPLIFIER_AMB_GRANULARITY", "drawer")
    monkeypatch.setenv("AMPLIFIER_AMB_RERANK", "on")
    monkeypatch.setenv("AMPLIFIER_AMB_DATES", "off")
    p = AmplifierMemoryProvider(embedder="none")
    assert p._granularity == "drawer"
    assert p._rerank is True
    assert p._dates is False

    monkeypatch.setenv("AMPLIFIER_AMB_GRANULARITY", "bogus")
    with pytest.raises(ValueError):
        AmplifierMemoryProvider(embedder="none")

    monkeypatch.setenv("AMPLIFIER_AMB_GRANULARITY", "passage")
    monkeypatch.setenv("AMPLIFIER_AMB_RERANK", "bogus")
    with pytest.raises(ValueError):
        AmplifierMemoryProvider(embedder="none")


class _ReadyEmbedder:
    ready = True
    model_id = "fake-model:3"

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def test_embedding_model_and_passages_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMPLIFIER_AMB_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("AMPLIFIER_AMB_PASSAGES", "off")
    p = AmplifierMemoryProvider(embedder="none")
    assert p._embedding_model == "BAAI/bge-small-en-v1.5"
    assert p._passages is False

    monkeypatch.delenv("AMPLIFIER_AMB_EMBEDDING_MODEL")
    monkeypatch.delenv("AMPLIFIER_AMB_PASSAGES")
    p = AmplifierMemoryProvider(embedder="none")
    assert p._embedding_model is None
    assert p._passages is True


def test_ingest_hands_embedder_and_model_id_to_store(tmp_path: Path) -> None:
    """Mirrors the daemon's remember path: passages of long drawers are
    embedded on write, and the drawer records which model embedded it."""
    provider = AmplifierMemoryProvider(home=tmp_path / "s", embedder="auto")
    provider._embedder = _ReadyEmbedder()
    provider._embedder_load_attempted = True

    class _Store(_FakeGranularityStore):
        def file(self, *, embedding_model_id=None, embedder=None, **kwargs):
            return super().file(
                embedding_model_id=embedding_model_id, embedder=embedder, **kwargs
            )

    fake = _Store()
    provider._store = fake

    provider.ingest([Document(id="x", content="hello", user_id="alice")])

    _, kwargs = fake.filed[0]
    assert kwargs["embedder"] is provider._embedder
    assert kwargs["embedding_model_id"] == "fake-model:3"


def test_passages_off_creates_no_passage_cells(tmp_path: Path) -> None:
    long_text = "\n".join(f"line {i} about the harbor schedule" for i in range(200))
    on = AmplifierMemoryProvider(home=tmp_path / "on", embedder="none")
    off = AmplifierMemoryProvider(
        home=tmp_path / "off", embedder="none", passages=False
    )
    for p in (on, off):
        p.ingest([Document(id="long", content=long_text, user_id="alice")])
    hits_on = on._store.search(None, 5, wing="amb_alice", lexical_query="harbor")
    hits_off = off._store.search(None, 5, wing="amb_alice", lexical_query="harbor")
    assert any(h.get("layer") == "passage" for h in hits_on)
    assert not any(h.get("layer") == "passage" for h in hits_off)
    on.cleanup()
    off.cleanup()
