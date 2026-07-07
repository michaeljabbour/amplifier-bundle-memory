"""
Tests for the authenticated MCP gateway over amplifier-data.

Covers token management, bearer auth (401 on missing/wrong token), localhost
bypass, the MCP round-trip (write/scope/fact/regenerate), and the
AmplifierDataMemoryStore(base_url, token) integration.

Auth-rejection cases construct the gateway with allow_localhost_bypass=False,
because the test client connects from 127.0.0.1 (which would otherwise bypass).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_data import AmplifierStore
from amplifier_data.models import Event  # noqa: E402

from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (  # noqa: E402
    GatewayClient,
    ensure_token,
    make_gateway,
)
from amplifier_module_tool_mempalace.scripts.memory_store import (  # noqa: E402
    AmplifierDataMemoryStore,
)

_TOKEN = "test-token-abc123"


def _serve(store: AmplifierStore, *, bypass: bool) -> Iterator[str]:
    httpd = make_gateway(
        store, "127.0.0.1", 0, token=_TOKEN, allow_localhost_bypass=bypass
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _post(url: str, body: dict[str, object], token: str | None) -> int:
    req = urllib.request.Request(
        url + "/mcp", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ---------------------------------------------------------------------------
# token management
# ---------------------------------------------------------------------------


class TestEnsureToken:
    def test_generates_and_persists(self, tmp_path: Path) -> None:
        p = tmp_path / "tok"
        t1 = ensure_token(p)
        assert t1 and p.read_text().strip() == t1
        assert (p.stat().st_mode & 0o777) == 0o600

    def test_idempotent(self, tmp_path: Path) -> None:
        p = tmp_path / "tok"
        assert ensure_token(p) == ensure_token(p)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class TestGatewayAuth:
    def test_missing_token_rejected(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            assert (
                _post(url, {"tool": "query_facts", "arguments": {}}, token=None) == 401
            )
        finally:
            next(gen, None)

    def test_wrong_token_rejected(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            assert (
                _post(url, {"tool": "query_facts", "arguments": {}}, token="nope")
                == 401
            )
        finally:
            next(gen, None)

    def test_correct_token_accepted(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            assert (
                _post(url, {"tool": "query_facts", "arguments": {}}, token=_TOKEN)
                == 200
            )
        finally:
            next(gen, None)

    def test_health_is_public(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            with urllib.request.urlopen(url + "/health", timeout=10) as resp:
                assert resp.status == 200 and json.loads(resp.read())["ok"] is True
        finally:
            next(gen, None)

    def test_localhost_bypass_allows_no_token(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=True))
        try:
            client = GatewayClient(url, token=None)
            ref = client.write_cell(b"bypass works")
            assert client.regenerate(ref).payload == b"bypass works"
        finally:
            next(gen, None)


# ---------------------------------------------------------------------------
# MCP round-trip
# ---------------------------------------------------------------------------


class TestGatewayRoundTrip:
    def test_full_round_trip_via_client(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            client = GatewayClient(url, _TOKEN)
            ref = client.write_cell("decided: keep it verbatim 世界".encode())
            assert (
                client.regenerate(ref).payload
                == "decided: keep it verbatim 世界".encode()
            )
            client.scope(ref, client.write_cell(b"wing:w"))
            labels = {
                client.regenerate(n).payload.decode()
                for n in client.graph_neighbors(ref, "scoped_to")
            }
            assert labels == {"wing:w"}
            cat = client.write_cell(b"decision")
            client.assert_fact(ref, "has_category", cat)
            facts = client.query_facts(subject=ref, predicate="has_category")
            assert facts.success and len(facts.output) == 1
            assert facts.output[0].object == cat
            client.invalidate_fact(ref, "has_category", cat)
            assert (
                client.query_facts(subject=ref, predicate="has_category").output == []
            )
        finally:
            next(gen, None)

    def test_memory_store_through_gateway(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            mem = AmplifierDataMemoryStore(base_url=url, token=_TOKEN)
            mem.file(
                wing="w",
                room="r",
                content="gateway drawer",
                category="decision",
                importance=0.75,
            )
            ref = mem.filed[-1]["ref"]
            assert mem.store.regenerate(ref).payload == b"gateway drawer"
            labels = {
                mem.store.regenerate(n).payload.decode()
                for n in mem.store.graph_neighbors(ref, "scoped_to")
            }
            assert labels == {"wing:w", "room:r"}
            facts = mem.store.query_facts(subject=ref, predicate="has_category")
            assert facts.success and len(facts.output) == 1
        finally:
            next(gen, None)


# ---------------------------------------------------------------------------
# KG-G1 -- gateway parity: add_embedding / query_vector / batch
# ---------------------------------------------------------------------------


class TestGatewayVectorAndBatchParity:
    def test_add_embedding_and_query_vector_round_trip(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            client = GatewayClient(url, _TOKEN)
            ref = client.write_cell(b"vector target")
            v = [1.0, 0.0, 0.0]
            emb_ref = client.add_embedding(ref, v)
            assert emb_ref
            hits = client.query_vector(v, k=1)
            assert hits and hits[0][0] == ref
        finally:
            next(gen, None)

    def test_query_vector_scoped(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            client = GatewayClient(url, _TOKEN)
            ref = client.write_cell(b"scoped vector target")
            scope_ref = client.write_cell(b"wing:vec_w")
            client.scope(ref, scope_ref)
            v = [0.0, 1.0, 0.0]
            client.add_embedding(ref, v)
            hits = client.query_vector(v, k=1, scope=scope_ref)
            assert hits and hits[0][0] == ref
            other_scope = client.write_cell(b"wing:vec_other")
            assert client.query_vector(v, k=1, scope=other_scope) == []
        finally:
            next(gen, None)

    def test_add_embedding_requires_auth(self, tmp_path: Path) -> None:
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        url = next(gen := _serve(store, bypass=False))
        try:
            assert (
                _post(
                    url,
                    {
                        "tool": "add_embedding",
                        "arguments": {"target_ref": "x", "vector": [1.0]},
                    },
                    token=None,
                )
                == 401
            )
        finally:
            next(gen, None)

    def test_batch_is_one_atomic_commit(self, tmp_path: Path) -> None:
        """KG-G1: a batch through the gateway is atomic per KG-A1(i) semantics
        server-side -- exactly one append_batch call for the whole batch."""
        store = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
        calls = {"n": 0}
        orig = store.kernel.append_batch

        def counting(events: list[Event]) -> list[Any]:
            calls["n"] += 1
            return orig(events)

        store.kernel.append_batch = counting  # type: ignore[method-assign]
        url = next(gen := _serve(store, bypass=False))
        try:
            client = GatewayClient(url, _TOKEN)
            batch = client.write_batch()
            content_ref = batch.write_cell(b"batched content")
            wing_ref = batch.write_cell(b"wing:batch_w")
            batch.relate(content_ref, wing_ref, "scoped_to")
            cat_ref = batch.write_cell(b"decision")
            batch.assert_fact(content_ref, "has_category", cat_ref)
            refs = batch.commit()
            assert calls["n"] == 1

            resolved_content = refs[content_ref] if isinstance(refs, dict) else content_ref
            direct_ref = client.write_cell(b"batched content")
            assert resolved_content == direct_ref
            assert client.regenerate(direct_ref).payload == b"batched content"
            labels = {
                client.regenerate(n).payload.decode()
                for n in client.graph_neighbors(direct_ref, "scoped_to")
            }
            assert "wing:batch_w" in labels
            facts = client.query_facts(subject=direct_ref, predicate="has_category")
            assert facts.success and len(facts.output) == 1
        finally:
            next(gen, None)

