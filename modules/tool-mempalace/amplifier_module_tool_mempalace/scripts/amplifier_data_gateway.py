"""
Authenticated MCP-style gateway over an amplifier-data store.

amplifier-data's companion server is localhost + no auth (CONSUMER_INTEGRATION
§5: "isolation/authz is the consumer's wrapper"). This module is that wrapper,
self-contained in the memory bundle and stdlib-only (no new deps):

  * one ``AmplifierStore`` behind a ThreadingHTTPServer (single-writer: a global
    lock serializes mutating ops, same guarantee the native server gives);
  * bearer-token auth with **socket-level localhost bypass** (unforgeable —
    uses the TCP peer IP, not headers) and constant-time token comparison;
  * an **MCP-shaped** endpoint: ``POST /mcp`` with ``{"tool", "arguments"}`` —
    the same call shape memory already uses for ``mempalace mcp --call``.

`GatewayClient` mirrors the subset of the store API that AmplifierDataMemoryStore
and the §8 harnesses need, sending the auth header.

Auth model (auth-tls-patterns skill): localhost "just works"; remote requires
the token. Token auto-generates to a 0600 file on first use.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import secrets
import sys
import threading
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_LOCALHOST = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_DEFAULT_TOKEN_PATH = Path.home() / ".amplifier" / "amplifier-data-token"

#: Reserved-type invalidate prefix (CONSUMER_INTEGRATION §2), duplicated here
#: (not imported from amplifier_data) so GatewayClient stays stdlib-only — a
#: process driving the gateway client never needs amplifier-data installed.
_INVALIDATE_PREFIX = "__invalidate__:"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def ensure_token(path: Path | str | None = None) -> str:
    """Return the existing auth token, or generate + persist a new one (0600)."""
    tok_path = Path(path) if path is not None else _DEFAULT_TOKEN_PATH
    if tok_path.exists():
        existing = tok_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    tok_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tok_path.write_text(token + "\n", encoding="utf-8")
    tok_path.chmod(0o600)
    return token


# ---------------------------------------------------------------------------
# Lightweight result shapes (mirror the fields our code reads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cell:
    payload: bytes


@dataclass(frozen=True)
class _Fact:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True)
class _FactResult:
    success: bool
    output: list[_Fact]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def make_gateway(
    store: Any,
    host: str,
    port: int,
    *,
    token: str,
    allow_localhost_bypass: bool = True,
) -> ThreadingHTTPServer:
    """Build (but do not start) an authenticated MCP gateway over ``store``."""

    lock = threading.Lock()

    def _dispatch(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "write_cell":
            payload = base64.b64decode(args["payload_b64"])
            with lock:
                ref = store.write_cell(payload)
            return {"ref": str(ref)}
        if tool == "scope":
            with lock:
                store.scope(args["cell_ref"], args["scope_ref"])
            return {"ok": True}
        if tool in ("assert_fact", "invalidate_fact"):
            with lock:
                getattr(store, tool)(args["subject"], args["predicate"], args["object"])
            return {"ok": True}
        if tool == "regenerate":
            cell = store.regenerate(args["ref"], record_access=False)
            return {"payload_b64": base64.b64encode(cell.payload).decode()}
        if tool == "graph_neighbors":
            neighbors = store.graph_neighbors(
                args["ref"], rel_type=args.get("rel_type")
            )
            return {"neighbors": [str(n) for n in neighbors]}
        if tool == "query_facts":
            res = store.query_facts(
                subject=args.get("subject"), predicate=args.get("predicate")
            )
            return {
                "success": bool(res.success),
                "output": [
                    {
                        "subject": str(f.subject),
                        "predicate": str(f.predicate),
                        "object": str(f.object),
                    }
                    for f in res.output
                ],
            }
        if tool == "add_embedding":
            with lock:
                ref = store.add_embedding(args["target_ref"], list(args["vector"]))
            return {"ref": str(ref)}
        if tool == "query_vector":
            # Read op, consistent with the existing read dispatch (query_facts,
            # graph_neighbors) -- no lock needed (D4: no AccessEvents either).
            hits = store.query_vector(
                list(args["vector"]), args["k"], scope=args.get("scope")
            )
            return {"results": [[str(ref), float(score)] for ref, score in hits]}
        if tool == "batch":
            # One HTTP call = one atomic commit. Ops reference each other via
            # client-minted pending tokens (opaque strings) resolved here as
            # write_cell ops execute in order -- the server never trusts the
            # client to have computed a real content-addressed ref itself.
            with lock:
                wb = store.write_batch()
                token_map: dict[str, str] = {}

                def _resolve(ref: str) -> str:
                    return token_map.get(ref, ref)

                for op in args["ops"]:
                    kind = op["op"]
                    if kind == "write_cell":
                        payload = base64.b64decode(op["payload_b64"])
                        ref = wb.write_cell(payload)
                        token_map[op["token"]] = str(ref)
                    elif kind == "relate":
                        wb.relate(
                            _resolve(op["from_ref"]),
                            _resolve(op["to_ref"]),
                            op["rel_type"],
                        )
                    elif kind == "assert_fact":
                        wb.assert_fact(
                            _resolve(op["subject"]),
                            op["predicate"],
                            _resolve(op["object"]),
                        )
                    elif kind == "scope":
                        wb.scope(_resolve(op["cell_ref"]), _resolve(op["scope_ref"]))
                    else:
                        raise ValueError(f"unknown batch op: {kind}")
                wb.commit()
            return {"refs": token_map}
        raise ValueError(f"unknown tool: {tool}")

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:  # silence default stderr logging
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            peer = self.client_address[0]
            if allow_localhost_bypass and peer in _LOCALHOST:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return hmac.compare_digest(header[7:], token)
            return False

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "service": "amplifier-data-gateway"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/mcp":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = _dispatch(payload["tool"], payload.get("arguments") or {})
            except Exception as exc:  # malformed request or dispatch error
                self._send(400, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(200, result)

    return ThreadingHTTPServer((host, port), _Handler)


# ---------------------------------------------------------------------------
# Client (authed) — mirrors the store subset our code uses
# ---------------------------------------------------------------------------


class GatewayClient:
    """Authed client for the MCP gateway. Method surface matches RemoteStore."""

    def __init__(
        self, base_url: str, token: str | None = None, *, timeout: float = 15.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"tool": tool, "arguments": arguments}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - localhost gateway
            f"{self.base_url}/mcp", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read())

    def write_cell(self, payload: bytes, interpreters: tuple[Any, ...] = ()) -> str:
        return self._call(
            "write_cell", {"payload_b64": base64.b64encode(payload).decode()}
        )["ref"]

    def scope(self, cell_ref: str, scope_ref: str) -> None:
        self._call("scope", {"cell_ref": cell_ref, "scope_ref": scope_ref})

    def assert_fact(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        self._call(
            "assert_fact",
            {"subject": subject, "predicate": predicate, "object": object},
        )

    def invalidate_fact(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        self._call(
            "invalidate_fact",
            {"subject": subject, "predicate": predicate, "object": object},
        )

    def regenerate(self, ref: str, *, record_access: bool | None = None) -> _Cell:
        out = self._call("regenerate", {"ref": ref})
        return _Cell(payload=base64.b64decode(out["payload_b64"]))

    def graph_neighbors(self, ref: str, rel_type: str | None = None) -> list[str]:
        return self._call("graph_neighbors", {"ref": ref, "rel_type": rel_type})[
            "neighbors"
        ]

    def query_facts(
        self, subject: str | None = None, predicate: str | None = None
    ) -> _FactResult:
        out = self._call("query_facts", {"subject": subject, "predicate": predicate})
        return _FactResult(
            success=out["success"],
            output=[
                _Fact(f["subject"], f["predicate"], f["object"]) for f in out["output"]
            ],
        )

    def add_embedding(self, target_ref: str, vector: Any) -> str:
        return self._call(
            "add_embedding", {"target_ref": target_ref, "vector": list(vector)}
        )["ref"]

    def query_vector(
        self, vector: Any, k: int, scope: str | None = None
    ) -> list[tuple[str, float]]:
        out = self._call(
            "query_vector", {"vector": list(vector), "k": k, "scope": scope}
        )
        return [(ref, float(score)) for ref, score in out["results"]]

    def write_batch(self) -> "GatewayWriteBatch":
        """Open a :class:`GatewayWriteBatch` — one HTTP call = one atomic commit.

        Mirrors :meth:`amplifier_data.store.AmplifierStore.write_batch`'s
        surface (write_cell/relate/assert_fact/scope/commit) without
        replicating the substrate's content-addressing client-side: staged
        ``write_cell`` calls return opaque pending tokens, resolved to real
        refs by the server as it executes the batch in order.
        """
        return GatewayWriteBatch(self)


class GatewayWriteBatch:
    """Client-side staging shim for the gateway's atomic ``batch`` tool.

    Refs from staged ``write_cell`` calls are opaque pending tokens (NOT real
    content-addressed hashes — computing those client-side would replicate
    the substrate's addressing algorithm, which the design deliberately
    avoids). Tokens are valid as ``relate``/``assert_fact``/``scope``
    arguments within the SAME batch; :meth:`commit` resolves them to real
    refs and returns ``{token: ref}``.
    """

    def __init__(self, client: "GatewayClient") -> None:
        self._client = client
        self._ops: list[dict[str, Any]] = []
        self._next_token = 0

    def write_cell(self, payload: bytes, interpreters: tuple[Any, ...] = ()) -> str:
        token = f"$pending:{self._next_token}"
        self._next_token += 1
        self._ops.append(
            {
                "op": "write_cell",
                "token": token,
                "payload_b64": base64.b64encode(payload).decode(),
            }
        )
        return token

    def relate(self, from_ref: str, to_ref: str, rel_type: str) -> "GatewayWriteBatch":
        self._ops.append(
            {"op": "relate", "from_ref": from_ref, "to_ref": to_ref, "rel_type": rel_type}
        )
        return self

    def assert_fact(self, subject: str, predicate: str, object: str) -> "GatewayWriteBatch":  # noqa: A002
        self._ops.append(
            {"op": "assert_fact", "subject": subject, "predicate": predicate, "object": object}
        )
        return self

    def scope(self, cell_ref: str, scope_ref: str) -> "GatewayWriteBatch":
        self._ops.append({"op": "scope", "cell_ref": cell_ref, "scope_ref": scope_ref})
        return self

    @property
    def staged(self) -> list[dict[str, Any]]:
        return list(self._ops)

    def __len__(self) -> int:
        return len(self._ops)

    def commit(self) -> dict[str, str]:
        """Commit every staged op in ONE atomic HTTP call; return ``{token: ref}``.

        A no-op (returns ``{}``) when nothing was staged — mirrors
        :meth:`amplifier_data.envelope.WriteBatch.commit`.
        """
        if not self._ops:
            return {}
        out = self._client._call("batch", {"ops": self._ops})
        return out["refs"]


# ---------------------------------------------------------------------------
# Launcher — run the gateway as a real service
# ---------------------------------------------------------------------------


def run_server(
    *,
    path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    token_path: str | None = None,
    allow_localhost_bypass: bool = True,
    record_access: bool = False,
) -> tuple[ThreadingHTTPServer, dict[str, Any]]:
    """Build (not start) a gateway over a durable (or in-memory) AmplifierStore.

    Returns (httpd, info) where info carries the resolved url, port, token_file.
    Pass ``path`` for a durable store (needs the Rust kernel); omit for in-memory.
    """
    from amplifier_data import AmplifierStore

    token = ensure_token(token_path)
    store = AmplifierStore(path=path, record_access=record_access)
    httpd = make_gateway(
        store, host, port, token=token, allow_localhost_bypass=allow_localhost_bypass
    )
    chosen = httpd.server_address[1]
    resolved_token = str(Path(token_path) if token_path else _DEFAULT_TOKEN_PATH)
    info = {
        "host": host,
        "port": chosen,
        "url": f"http://{host}:{chosen}",
        "token_file": resolved_token,
        "durable": path is not None,
        "localhost_bypass": allow_localhost_bypass,
    }
    return httpd, info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mempalace-amplifier-data-gateway")
    parser.add_argument(
        "--path", default=None, help="durable store path (omit = in-memory)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = OS-assigned")
    parser.add_argument(
        "--token-file", default=None, help="bearer-token file (auto-gen, 0600)"
    )
    parser.add_argument(
        "--no-localhost-bypass",
        action="store_true",
        help="require the token even from localhost",
    )
    parser.add_argument("--record-access", action="store_true")
    args = parser.parse_args(argv)

    httpd, info = run_server(
        path=args.path,
        host=args.host,
        port=args.port,
        token_path=args.token_file,
        allow_localhost_bypass=not args.no_localhost_bypass,
        record_access=args.record_access,
    )
    sys.stdout.write(json.dumps(info) + "\n")  # discovery line for callers
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
