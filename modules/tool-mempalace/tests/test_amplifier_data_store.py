"""
Verify AmplifierDataMemoryStore against the REAL amplifier-data library.

These are the §8 regeneration-equivalence checks promoted into the test suite:
verbatim content written through the store regenerates byte-for-byte (E1), scope
membership lands as `scoped_to` edges, and category/importance land as queryable
KG facts. Skipped when amplifier-data is not installed (it is an optional dep).

`file()` returns None (uniform MemoryStore contract); the content ref is exposed
via `store.filed[-1]["ref"]`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_mempalace.scripts.memory_store import (  # noqa: E402
    AmplifierDataMemoryStore,
)


def _file(store: AmplifierDataMemoryStore, **kw: Any) -> str:
    store.file(**kw)
    return store.filed[-1]["ref"]  # type: ignore[return-value]


def test_content_regenerates_byte_identical() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    content = "We decided to go with the manifest. 世界\nline2"
    ref = _file(
        store,
        wing="wing_demo",
        room="auth",
        content=content,
        category="decision",
        importance=0.75,
    )
    assert store.store.regenerate(ref).payload == content.encode("utf-8")


def test_identical_content_dedups_to_same_ref() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    a = _file(store, wing="w", room="r", content="same bytes")
    b = _file(store, wing="w", room="r", content="same bytes")
    assert a == b  # sha256 content addressing


def test_scope_edges_are_present() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    ref = _file(store, wing="wing_a", room="room_x", content="scoped content")
    neighbors = store.store.graph_neighbors(ref, rel_type="scoped_to")
    labels = {store.store.regenerate(n).payload.decode() for n in neighbors}
    assert labels == {"wing:wing_a", "room:room_x"}


def test_category_and_importance_become_facts() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    s = store.store
    ref = _file(
        store, wing="w", room="r", content="x", category="pattern", importance=0.5
    )
    cat = s.query_facts(subject=ref, predicate="has_category")
    assert cat.success and len(cat.output) == 1
    assert s.regenerate(cat.output[0].object).payload == b"pattern"
    imp = s.query_facts(subject=ref, predicate="has_importance")
    assert imp.success and s.regenerate(imp.output[0].object).payload == b"0.5"


def test_no_facts_when_uncategorized() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    ref = _file(store, wing="w", room="r", content="plain")
    res = store.store.query_facts(subject=ref, predicate="has_category")
    assert res.output == []


def test_invalidated_fact_disappears_then_revalidates() -> None:
    # temporal validity: invalidate removes the fact; re-asserting brings it back
    store = AmplifierDataMemoryStore(record_access=False)
    s = store.store
    ref = _file(store, wing="w", room="r", content="x", category="decision")
    cat_obj = s.query_facts(subject=ref, predicate="has_category").output[0].object
    s.invalidate_fact(ref, "has_category", cat_obj)
    assert s.query_facts(subject=ref, predicate="has_category").output == []
    s.assert_fact(ref, "has_category", cat_obj)
    assert len(s.query_facts(subject=ref, predicate="has_category").output) == 1


def test_durable_restart_regenerates_identical(tmp_path: Path) -> None:
    path = tmp_path / "store.ampd"
    s1 = AmplifierDataMemoryStore(path=str(path), record_access=False)
    ref = _file(s1, wing="w", room="r", content="durable verbatim 世界")
    s1.store.close()

    s2 = AmplifierDataMemoryStore(path=str(path), record_access=False)
    assert s2.store.regenerate(ref).payload == "durable verbatim 世界".encode("utf-8")
    s2.store.close()


def test_filed_records_tracked() -> None:
    store = AmplifierDataMemoryStore(record_access=False)
    store.file(wing="w", room="r", content="a", category="decision", importance=0.75)
    assert len(store.filed) == 1
    rec = store.filed[0]
    assert rec["wing"] == "w" and rec["category"] == "decision"
    assert "ref" in rec


def test_remote_store_via_companion_server(tmp_path: Path) -> None:
    """The store works through the single-writer companion server (RemoteStore)."""
    import threading

    from amplifier_data import AmplifierStore
    from amplifier_data import server as srv

    backing = AmplifierStore(path=str(tmp_path / "srv.ampd"), record_access=False)
    httpd = srv.make_server(backing, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        store = AmplifierDataMemoryStore(base_url=f"http://127.0.0.1:{port}")
        store.file(wing="w", room="r", content="remote verbatim", category="decision")
        ref = store.filed[-1]["ref"]
        s = store.store
        assert s.regenerate(ref).payload == b"remote verbatim"
        labels = {
            s.regenerate(n).payload.decode()
            for n in s.graph_neighbors(ref, rel_type="scoped_to")
        }
        assert labels == {"wing:w", "room:r"}
        facts = s.query_facts(subject=ref, predicate="has_category")
        assert facts.success and len(facts.output) == 1
    finally:
        httpd.shutdown()
