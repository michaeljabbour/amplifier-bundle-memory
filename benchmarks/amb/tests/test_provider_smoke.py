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
