"""Tests for the memory daemon's \u00a75.4 domain tools and \u00a74.3 embedder degradation,
served over the REAL HTTP dispatch layer (make_daemon), against a real
(in-memory) AmplifierStore. Covers "daemon domain-tool round-trips" (B1 gate)
and KG-N3 (embedder-offline degraded mode, the daemon half -- \u00a75.7's client
half is covered in test_client_ensure_daemon.py).

Skipped entirely when amplifier-data is not installed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.embedder import (  # noqa: E402
    EmbedderUnavailable,
)
from amplifier_module_tool_memory.daemon import (  # noqa: E402
    daemon_version,
    make_daemon,
)

from amplifier_data import AmplifierStore  # noqa: E402

_TOKEN = "daemon-test-token"


class _FakeEmbedder:
    """Deterministic stand-in for FastEmbedEmbedder -- no model download needed."""

    def __init__(self, *, ready: bool = True, dim: int = 3) -> None:
        self._ready = ready
        self.failed: str | None = None if ready else "forced offline for test"
        self.dim = dim

    @property
    def ready(self) -> bool:
        return self._ready

    def embed(self, text: str) -> list[float]:
        if not self._ready:
            raise EmbedderUnavailable(self.failed or "not ready")
        # Deterministic, content-derived vector so cosine ranking is meaningful.
        h = sum(text.encode("utf-8")) % 997
        return [float(h % 7), float(h % 5), float(h % 3)]


def _serve(store: Any, embedder: Any, *, durable: bool = False) -> Iterator[str]:
    httpd = make_daemon(
        store,
        embedder,
        "127.0.0.1",
        0,
        token=_TOKEN,
        version="9.9.9-test",
        durable=durable,
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _call(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"tool": tool, "arguments": arguments}).encode("utf-8")
    req = urllib.request.Request(f"{url}/mcp", data=body, method="POST")  # noqa: S310
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {_TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


class TestHealthShape:
    def test_health_reports_version_embedder_durable(self, tmp_path) -> None:  # noqa: ANN001
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            hc = _health(url)
            assert hc["ok"] is True
            assert hc["service"] == "memory-daemon"
            assert hc["version"] == "9.9.9-test"
            assert hc["embedder"] == {"ready": True, "failed": None}
            assert hc["durable"] is False
        finally:
            next(gen, None)

    def test_daemon_version_helper_resolves_something(self) -> None:
        # Editable/dev installs may not have package metadata -- the fallback
        # sentinel is acceptable, but it must never raise.
        assert isinstance(daemon_version(), str)
        assert daemon_version() != ""


class TestDomainToolRoundTrips:
    def test_remember_then_search_round_trip(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            out = _call(
                url,
                "remember",
                {"wing": "w", "room": "r", "content": "we decided on the manifest"},
            )
            assert out["ref"]

            search_out = _call(
                url, "search", {"query": "manifest", "k": 5, "wing": "w"}
            )
            assert search_out["degraded"] is None
            assert search_out["results"]
            assert search_out["results"][0]["ref"] == out["ref"]
            assert "manifest" in search_out["results"][0]["content"]
        finally:
            next(gen, None)

    def test_status(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            _call(url, "remember", {"wing": "sw", "room": "r", "content": "x"})
            st = _call(url, "status", {})
            assert st["drawers"] >= 1
            assert "sw" in st["wings"]
            assert st["embedder"] == {"ready": True, "failed": None}
        finally:
            next(gen, None)

    def test_kg_query_timeline_and_stats(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            _call(
                url,
                "kg_query",
                {"subject": "alice"},
            )  # exercise query path with no facts yet -- must not error
            # kg_query is read-only; assert via the seam's own assert_kg
            # convention is not exposed as a dispatch tool (write-side KG
            # facts arrive via curate.dot / migration in later phases), so
            # we drive it through the low-level generic tools instead.
            anchor_alice = _call(
                url, "write_cell", {"payload_b64": _b64("entity:alice")}
            )["ref"]
            anchor_bob = _call(url, "write_cell", {"payload_b64": _b64("entity:bob")})[
                "ref"
            ]
            _call(
                url,
                "assert_fact",
                {"subject": anchor_alice, "predicate": "knows", "object": anchor_bob},
            )
            kg = _call(url, "kg_query", {"subject": "alice"})
            assert kg["facts"] == [["alice", "knows", "bob"]]

            timeline = _call(url, "kg_timeline", {"subject": "alice"})
            assert len(timeline["entries"]) == 1
            assert timeline["entries"][0]["predicate"] == "knows"

            stats = _call(url, "kg_stats", {})
            assert stats["facts"] >= 1
            assert stats["entities"] >= 2
        finally:
            next(gen, None)

    def test_traverse(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            anchor_a = _call(url, "write_cell", {"payload_b64": _b64("entity:a")})[
                "ref"
            ]
            anchor_b = _call(url, "write_cell", {"payload_b64": _b64("entity:b")})[
                "ref"
            ]
            _call(
                url,
                "assert_fact",
                {"subject": anchor_a, "predicate": "linked_to", "object": anchor_b},
            )
            out = _call(url, "traverse", {"start": "a", "max_hops": 2})
            assert anchor_b in out["refs"]
        finally:
            next(gen, None)

    def test_diary_write_and_read(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            _call(
                url,
                "diary_write",
                {"agent_name": "curator", "entry": "first", "topic": "general"},
            )
            _call(
                url,
                "diary_write",
                {"agent_name": "curator", "entry": "second", "topic": "general"},
            )
            out = _call(url, "diary_read", {"agent_name": "curator", "last_n": 10})
            assert [e["entry"] for e in out["entries"]] == ["first", "second"]
        finally:
            next(gen, None)

    def test_list_drawers(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        url = next(gen := _serve(store, embedder))
        try:
            _call(url, "remember", {"wing": "ld", "room": "r", "content": "x"})
            _call(url, "remember", {"wing": "ld", "room": "r", "content": "y"})
            out = _call(url, "list_drawers", {"wing": "ld", "room": "r", "limit": 10})
            assert len(out["drawers"]) == 2
        finally:
            next(gen, None)

    def test_shutdown_stops_the_server(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=True)
        httpd = make_daemon(
            store, embedder, "127.0.0.1", 0, token=_TOKEN, version="9.9.9-test"
        )
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}"

        out = _call(url, "shutdown", {})
        assert out["ok"] is True

        import time

        deadline = time.monotonic() + 5.0
        stopped = False
        while time.monotonic() < deadline:
            try:
                _health(url)
                time.sleep(0.1)
            except (urllib.error.URLError, ConnectionError, OSError):
                stopped = True
                break
        assert stopped, "daemon did not stop after the shutdown tool was invoked"


class TestEmbedderDegradedMode:
    """KG-N3 (daemon half): remember succeeds + needs_embedding, search is
    lexical-only + degraded flag, no exception reaches the caller."""

    def test_remember_without_ready_embedder_marks_needs_embedding(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=False)
        url = next(gen := _serve(store, embedder))
        try:
            out = _call(
                url,
                "remember",
                {"wing": "w", "room": "r", "content": "exact keyword hit content"},
            )
            ref = out["ref"]
            fact = store.query_facts(subject=ref, predicate="needs_embedding")
            assert fact.success and len(fact.output) == 1
        finally:
            next(gen, None)

    def test_search_without_ready_embedder_is_degraded_lexical_hit(self) -> None:
        store = AmplifierStore(record_access=False)
        embedder = _FakeEmbedder(ready=False)
        url = next(gen := _serve(store, embedder))
        try:
            _call(
                url,
                "remember",
                {"wing": "w2", "room": "r", "content": "exact keyword hit content"},
            )
            out = _call(url, "search", {"query": "keyword hit", "k": 5, "wing": "w2"})
            assert out["degraded"] == "lexical_only"
            assert out["results"]
            assert "keyword" in out["results"][0]["content"]
        finally:
            next(gen, None)

    def test_embedder_none_reports_disabled_in_status(self) -> None:
        store = AmplifierStore(record_access=False)
        url = next(gen := _serve(store, None))
        try:
            st = _call(url, "status", {})
            assert st["embedder"]["ready"] is False
            hc = _health(url)
            assert hc["embedder"]["ready"] is False
        finally:
            next(gen, None)


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode()
