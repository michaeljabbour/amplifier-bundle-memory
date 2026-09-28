"""P2 -- daemon + client round trips for fact_add/facts/search(layers) and
the reflection job queue tools, served over the REAL HTTP dispatch layer
(``make_daemon``), mirroring test_daemon.py's conventions.

Skipped entirely when amplifier-data is not installed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_data import AmplifierStore
from amplifier_module_tool_memory.client import MemoryClient
from amplifier_module_tool_memory.daemon import make_daemon
from amplifier_module_tool_memory.embedder import EmbedderUnavailable

_TOKEN = "p2-test-token"


class _FakeEmbedder:
    def __init__(self, *, ready: bool = True) -> None:
        self._ready = ready
        self.failed: str | None = None if ready else "forced offline for test"

    @property
    def ready(self) -> bool:
        return self._ready

    def embed(self, text: str) -> list[float]:
        if not self._ready:
            raise EmbedderUnavailable(self.failed or "not ready")
        h = sum(text.encode("utf-8")) % 997
        return [float(h % 7), float(h % 5), float(h % 3)]


def _serve(store: Any, embedder: Any) -> Iterator[MemoryClient]:
    httpd = make_daemon(
        store, embedder, "127.0.0.1", 0, token=_TOKEN, version="p2-test", durable=False
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield MemoryClient(f"http://127.0.0.1:{port}", _TOKEN)
    finally:
        httpd.shutdown()


class TestFactAddFactsRoundTrip:
    def test_fact_add_then_facts_via_client(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            drawer_ref = client.remember(
                wing="w", room="r", content="the repo uses uv not pip for installs"
            )
            out = client.fact_add(
                text=(
                    "The repository uses uv as its package manager instead "
                    "of pip for installs."
                ),
                fact_type="world",
                source_refs=[drawer_ref],
                wing="w",
                room="r",
                predicate="package_manager",
            )
            assert out["deduped"] is False
            assert out["proof_count"] == 1

            rows = client.facts(wing="w", room="r")
            assert len(rows) == 1
            assert rows[0]["ref"] == out["ref"]
            assert rows[0]["derived_from"] == [drawer_ref]
        finally:
            next(gen, None)

    def test_fact_add_rejects_missing_provenance_as_daemon_error(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            with pytest.raises(Exception):  # noqa: B017 - transport-level 400
                client.fact_add(
                    text="A fact with no evidence at all backing it up here.",
                    fact_type="world",
                    source_refs=[],
                    wing="w",
                )
        finally:
            next(gen, None)

    def test_embedder_offline_marks_needs_embedding_on_fact(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=False)))
        try:
            drawer_ref = client.remember(wing="w", room="r", content="evidence text")
            out = client.fact_add(
                text=(
                    "An offline-embedder fact used to prove the needs_embedding "
                    "marker gets asserted correctly."
                ),
                fact_type="world",
                source_refs=[drawer_ref],
                wing="w",
                room="r",
            )
            fact = store.query_facts(subject=out["ref"], predicate="needs_embedding")
            assert fact.success and len(fact.output) == 1
        finally:
            next(gen, None)


class TestSearchLayers:
    def test_search_default_returns_both_layers(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            drawer_ref = client.remember(
                wing="w", room="r", content="the repo uses uv not pip"
            )
            fact_out = client.fact_add(
                text="The repository's package manager is uv, not pip, for installs.",
                fact_type="world",
                source_refs=[drawer_ref],
                wing="w",
                room="r",
            )
            out = client.search("package manager", 5, wing="w")
            layers = {r["ref"]: r["layer"] for r in out["results"]}
            assert layers.get(fact_out["ref"]) == "fact"
        finally:
            next(gen, None)

    def test_search_layers_drawer_only(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            drawer_ref = client.remember(
                wing="w", room="r", content="the repo uses uv not pip"
            )
            client.fact_add(
                text="The repository's package manager is uv, not pip, for installs.",
                fact_type="world",
                source_refs=[drawer_ref],
                wing="w",
                room="r",
            )
            out = client.search("uv pip", 5, wing="w", layers=["drawer"])
            assert all(r["layer"] == "drawer" for r in out["results"])
            assert any(r["ref"] == drawer_ref for r in out["results"])
        finally:
            next(gen, None)


class TestReflectionJobRoundTrip:
    def test_add_list_done_via_client(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            job = client.reflection_job_add(
                span_text="the user asked to always use uv for this repo",
                session_id="sess-1",
                trigger="session:end",
                wing="w",
            )
            assert job["state"] == "pending"

            jobs = client.reflection_jobs(wing="w")
            assert [j["job_ref"] for j in jobs] == [job["job_ref"]]

            done = client.reflection_job_done(job_ref=job["job_ref"])
            assert done["state"] == "done"
            assert client.reflection_jobs(state="pending", wing="w") == []
        finally:
            next(gen, None)

    def test_done_twice_raises_via_client(self) -> None:
        store = AmplifierStore(record_access=False)
        client = next(gen := _serve(store, _FakeEmbedder(ready=True)))
        try:
            job = client.reflection_job_add(
                span_text="a span closed exactly once through the client",
                session_id=None,
                trigger="session:end",
                wing="w",
            )
            client.reflection_job_done(job_ref=job["job_ref"])
            with pytest.raises(Exception):  # noqa: B017 - transport-level 400
                client.reflection_job_done(job_ref=job["job_ref"])
        finally:
            next(gen, None)
