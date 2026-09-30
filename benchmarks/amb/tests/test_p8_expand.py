"""P8 (D34) "rank on passages, read on neighborhoods" -- the AMB adapter's
expand modes, aggregate-cue gating, and token cap.

Runs against the REAL ``NativeMemoryStore`` (embedder="none", lexical-only)
so passage splitting/expansion behaves exactly as production search does --
same convention as test_provider_smoke.py's end-to-end tests.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_fake_memory_bench() -> None:
    if "memory_bench" in sys.modules:
        return
    try:
        import memory_bench  # noqa: F401

        return
    except ImportError:
        pass

    from abc import ABC, abstractmethod
    from dataclasses import dataclass

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


_install_fake_memory_bench()

import pytest
from amplifier_memory_provider import AmplifierMemoryProvider
from memory_bench.models import Document

pytest.importorskip("amplifier_module_tool_memory.store")
pytest.importorskip("amplifier_data")


def _long_content(marker: str) -> str:
    lines = []
    for i in range(40):
        lines.append(f"user: filler turn {i} about the deploy pipeline")
        lines.append(f"assistant: acknowledged turn {i}")
        lines.append("")
    lines.append(f"assistant: {marker}")
    text = "\n".join(lines)
    assert len(text) >= 1200
    return text


@pytest.fixture
def provider_factory(tmp_path: Path):
    made: list[AmplifierMemoryProvider] = []

    def _make(**kwargs: Any) -> AmplifierMemoryProvider:
        p = AmplifierMemoryProvider(
            home=tmp_path / f"s{len(made)}", embedder="none", **kwargs
        )
        made.append(p)
        return p

    yield _make
    for p in made:
        p.cleanup()


def test_expand_defaults_to_none(provider_factory) -> None:
    provider = provider_factory()
    assert provider._expand == "none"
    provider.ingest(
        [Document(id="d1", content=_long_content("marker-none"), user_id="u")]
    )
    docs, _ = provider.retrieve("marker-none", k=5, user_id="u")
    assert docs
    assert provider.last_raw["expand"] == "none"


def test_expand_neighbors_widens_document_content(provider_factory) -> None:
    provider = provider_factory(expand="neighbors", expand_neighbors=1)
    content = _long_content("marker-neighbors-p8")
    provider.ingest([Document(id="d1", content=content, user_id="u")])

    docs, _ = provider.retrieve("marker-neighbors-p8", k=5, user_id="u")
    assert docs
    assert provider.last_raw["expand"] == "neighbors"
    assert provider.last_raw["expand_windows"] >= 1
    # The expanded document content must be a real substring of the
    # original drawer (dedup via re-slicing, not concatenation).
    assert docs[0].content.lstrip("[0123456789-] ") in content or any(
        line in content for line in docs[0].content.splitlines()
    )


def test_expand_drawer_returns_whole_document(provider_factory) -> None:
    provider = provider_factory(expand="drawer")
    content = _long_content("marker-drawer-p8")
    provider.ingest([Document(id="d1", content=content, user_id="u")])

    docs, _ = provider.retrieve("marker-drawer-p8", k=5, user_id="u")
    assert docs
    assert provider.last_raw["expand"] == "drawer"
    # Stripped of the date-stamp prefix, the body must equal the WHOLE
    # original drawer content exactly.
    body = docs[0].content
    if provider._dates and body.startswith("["):
        # "[YYYY-MM-DD] " prefix, when a filed_at stamp exists.
        body = body.split("] ", 1)[1]
    assert body == content


def test_expand_only_aggregate_gates_on_query_cue(provider_factory) -> None:
    provider = provider_factory(expand="neighbors", expand_only_aggregate=True)
    content = _long_content("marker-aggregate-p8")
    provider.ingest([Document(id="d1", content=content, user_id="u")])

    # No aggregate cue -- expansion must NOT apply.
    provider.retrieve("marker-aggregate-p8 details", k=5, user_id="u")
    assert provider.last_raw["expand"] == "none"

    # An aggregate cue ("how many") -- expansion DOES apply.
    provider.retrieve("how many times marker-aggregate-p8", k=5, user_id="u")
    assert provider.last_raw["expand"] == "neighbors"


def test_expand_only_aggregate_off_always_expands(provider_factory) -> None:
    provider = provider_factory(expand="neighbors", expand_only_aggregate=False)
    content = _long_content("marker-always-p8")
    provider.ingest([Document(id="d1", content=content, user_id="u")])
    provider.retrieve("marker-always-p8 plain query", k=5, user_id="u")
    assert provider.last_raw["expand"] == "neighbors"


def test_expand_tokens_env_and_char_budget_conversion(
    monkeypatch: pytest.MonkeyPatch, provider_factory
) -> None:
    monkeypatch.setenv("AMPLIFIER_AMB_EXPAND", "neighbors")
    monkeypatch.setenv("AMPLIFIER_AMB_EXPAND_TOKENS", "500")
    provider = provider_factory()
    assert provider._expand == "neighbors"
    assert provider._expand_tokens == 500

    import functools

    calls: list[dict] = []
    store = provider._ensure_store()
    real_search = store.search

    @functools.wraps(real_search)
    def _spy_search(*args, **kwargs):
        calls.append(kwargs)
        return real_search(*args, **kwargs)

    monkeypatch.setattr(store, "search", _spy_search)
    provider.ingest(
        [Document(id="d1", content=_long_content("marker-tok-p8"), user_id="u")]
    )
    provider.retrieve("marker-tok-p8", k=5, user_id="u")

    assert calls
    assert calls[0]["expand_char_budget"] == 500 * 4


def test_expand_env_var(monkeypatch: pytest.MonkeyPatch, provider_factory) -> None:
    monkeypatch.setenv("AMPLIFIER_AMB_EXPAND", "drawer")
    provider = provider_factory()
    assert provider._expand == "drawer"


def test_invalid_expand_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        AmplifierMemoryProvider(home=tmp_path / "s", embedder="none", expand="bogus")
