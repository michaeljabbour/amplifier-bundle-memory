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

from amplifier_data.models import Event

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


# ---------------------------------------------------------------------------
# KG-V1 / KG-V2 -- vector transport
# ---------------------------------------------------------------------------


def test_vector_top1_self_retrieval_and_scope_isolation() -> None:
    """KG-V1: file(embedding=v) with wing w -> search_vectors(v, k=1, wing=w)
    returns (ref, ~1.0) top-1; the same query scoped to a DIFFERENT wing
    returns []."""
    store = AmplifierDataMemoryStore(record_access=False)
    v = [1.0, 0.0, 0.0]
    ref = _file(store, wing="wing_v1", room="r", content="vector content", embedding=v)

    hits = store.search_vectors(v, k=1, wing="wing_v1")
    assert len(hits) == 1
    got_ref, score = hits[0]
    assert got_ref == ref
    assert score == pytest.approx(1.0, abs=1e-6)

    other = store.search_vectors(v, k=1, wing="wing_other")
    assert other == []


def test_embedding_regenerates_byte_identical_after_reopen(tmp_path: Path) -> None:
    """KG-V2: the embedding cell regenerates byte-identically as LE-f32 (struct
    round-trip equals input within f32 precision) after close()+reopen of a
    durable store; iter_embeddings() contains (ref, v)."""
    path = tmp_path / "vec.ampd"
    v = [0.25, -0.5, 1.5, 0.0]
    s1 = AmplifierDataMemoryStore(path=str(path), record_access=False)
    ref = _file(s1, wing="w", room="r", content="vec content", embedding=v)
    s1.store.close()

    s2 = AmplifierDataMemoryStore(path=str(path), record_access=False)
    embeddings = dict(s2.store.iter_embeddings())
    assert ref in embeddings
    got = embeddings[ref]
    assert len(got) == len(v)
    for a, b in zip(got, v, strict=True):
        assert abs(a - b) < 1e-6
    s2.store.close()


# ---------------------------------------------------------------------------
# KG-A1 / KG-A2 -- atomic batch adoption
# ---------------------------------------------------------------------------


def test_update_importance_atomic_success_and_crash_injection() -> None:
    """KG-A1: (i) append_batch called exactly ONCE for the whole update on a
    write_batch-capable backend; (ii) crash-injection -- forcing append_batch
    to raise leaves the OLD value intact (no invalidated-but-not-reasserted
    half-state); (iii) MutationRecord.atomic is True."""
    from amplifier_data import AmplifierStore
    from amplifier_data.kernel import InMemoryKernel

    backing = AmplifierStore(kernel=InMemoryKernel(), record_access=False)
    store = AmplifierDataMemoryStore(store=backing)
    ref = _file(store, wing="w", room="r", content="x", importance=0.5)

    calls = {"n": 0}
    orig = backing.kernel.append_batch

    def counting(events: list[Event]) -> list[Any]:
        calls["n"] += 1
        return orig(events)

    backing.kernel.append_batch = counting  # type: ignore[method-assign]
    record = store.update_importance(
        ref,
        old_importance=0.5,
        new_importance=0.9,
        provenance="test",
        source_outcome="test_outcome",
        confidence=0.8,
    )
    assert calls["n"] == 1
    assert record.atomic is True
    facts = backing.query_facts(subject=ref, predicate="has_importance")
    assert backing.regenerate(facts.output[0].object).payload == b"0.9"

    # (ii) crash injection: force append_batch to raise on the NEXT update.
    def raising(events: object) -> object:
        calls["n"] += 1
        raise RuntimeError("boom")

    backing.kernel.append_batch = raising  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.update_importance(
            ref,
            old_importance=0.9,
            new_importance=0.99,
            provenance="test",
            source_outcome="test_outcome",
            confidence=0.8,
        )
    facts_after = backing.query_facts(subject=ref, predicate="has_importance")
    assert backing.regenerate(facts_after.output[0].object).payload == b"0.9"


def test_update_importance_non_atomic_on_remote_store(tmp_path: Path) -> None:
    """KG-A1 (iv): on a RemoteStore-shaped stub (no write_batch), atomic is
    False and the sequential path still lands the update."""
    import threading

    from amplifier_data import AmplifierStore
    from amplifier_data import server as srv

    backing = AmplifierStore(path=str(tmp_path / "srv.ampd"), record_access=False)
    httpd = srv.make_server(backing, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        store = AmplifierDataMemoryStore(base_url=f"http://127.0.0.1:{port}")
        ref = _file(store, wing="w", room="r", content="remote imp", importance=0.5)
        record = store.update_importance(
            ref,
            old_importance=0.5,
            new_importance=0.7,
            provenance="test",
            source_outcome="test_outcome",
            confidence=0.8,
        )
        assert record.atomic is False
        facts = store.store.query_facts(subject=ref, predicate="has_importance")
        assert store.store.regenerate(facts.output[0].object).payload == b"0.7"
    finally:
        httpd.shutdown()


def test_file_atomic_single_append_batch_with_embedding() -> None:
    """KG-A2: file() with category+importance+embedding on a batch-capable
    backend produces exactly ONE append_batch containing cell + 2 scope
    edges + facts + embedding events; end state is lens-identical to the
    sequential path (same query_facts, graph_neighbors, query_vector answers)."""
    from amplifier_data import AmplifierStore
    from amplifier_data.kernel import InMemoryKernel

    backing = AmplifierStore(kernel=InMemoryKernel(), record_access=False)
    store = AmplifierDataMemoryStore(store=backing)

    calls = {"n": 0}
    orig = backing.kernel.append_batch

    def counting(events: list[Event]) -> list[Any]:
        calls["n"] += 1
        return orig(events)

    backing.kernel.append_batch = counting  # type: ignore[method-assign]
    v = [1.0, 2.0, 3.0]
    ref = _file(
        store,
        wing="w",
        room="r",
        content="atomic file",
        category="decision",
        importance=0.75,
        embedding=v,
    )
    assert calls["n"] == 1

    assert backing.regenerate(ref).payload == b"atomic file"
    labels = {
        backing.regenerate(n).payload.decode()
        for n in backing.graph_neighbors(ref, rel_type="scoped_to")
    }
    assert labels == {"wing:w", "room:r"}
    cat = backing.query_facts(subject=ref, predicate="has_category")
    assert cat.success and backing.regenerate(cat.output[0].object).payload == b"decision"
    imp = backing.query_facts(subject=ref, predicate="has_importance")
    assert imp.success and backing.regenerate(imp.output[0].object).payload == b"0.75"
    hits = store.search_vectors(v, k=1, wing="w")
    assert hits and hits[0][0] == ref


# ---------------------------------------------------------------------------
# KG-K1 / KG-K2 -- KG facts via anchor cells
# ---------------------------------------------------------------------------


def test_assert_kg_query_and_timeline() -> None:
    """KG-K1 (seam half): assert_kg -> query_kg contains the triple;
    invalidate_kg -> gone from query_kg but kg_timeline shows assert-then-
    invalidate (validity window, SeqPos-ordered)."""
    store = AmplifierDataMemoryStore(record_access=False)
    store.assert_kg("svc-a", "depends_on", "svc-b")
    facts = store.query_kg(subject="svc-a")
    assert ("svc-a", "depends_on", "svc-b") in facts

    store.invalidate_kg("svc-a", "depends_on", "svc-b")
    facts_after = store.query_kg(subject="svc-a")
    assert ("svc-a", "depends_on", "svc-b") not in facts_after

    timeline = store.kg_timeline("svc-a")
    ops = [t["op"] for t in timeline]
    assert ops == ["assert", "invalidate"]


def test_kg_phase3_shaped_facts_traverse_and_resolve() -> None:
    """KG-K2: Phase-3-shaped facts round-trip -- duplicates/related_to become
    anchor<->anchor facts traversable via graph_neighbors; has_importance /
    has_category value facts resolve back to their strings via query_kg."""
    store = AmplifierDataMemoryStore(record_access=False)
    store.assert_kg("drawer:1", "duplicates", "drawer:2")
    store.assert_kg("drawer:1", "has_importance", "0.75")
    store.assert_kg("drawer:1", "has_category", "decision")

    anchor1 = store._anchor("drawer:1")
    anchor2 = store._anchor("drawer:2")
    neighbors = store.store.graph_neighbors(anchor1, rel_type="duplicates")
    assert neighbors == [anchor2]

    facts = store.query_kg(subject="drawer:1")
    assert ("drawer:1", "has_importance", "0.75") in facts
    assert ("drawer:1", "has_category", "decision") in facts


# ---------------------------------------------------------------------------
# KG-D1 -- diary entries -> cells
# ---------------------------------------------------------------------------


def test_file_diary_scoped_and_sourced() -> None:
    """KG-D1: file_diary lands a cell reachable via the agent:<name> scope,
    regenerates byte-identical to the entry, and carries has_source =
    diary:<name>."""
    store = AmplifierDataMemoryStore(record_access=False)
    ref = store.file_diary(agent_name="curator", entry="today's diary entry", topic="t")

    assert store.store.regenerate(ref).payload == b"today's diary entry"
    agent_scope = store.store.write_cell(b"agent:curator")
    neighbors = store.store.graph_neighbors(ref, rel_type="scoped_to")
    assert agent_scope in neighbors

    src = store.store.query_facts(subject=ref, predicate="has_source")
    assert src.success
    assert store.store.regenerate(src.output[0].object).payload == b"diary:curator"

