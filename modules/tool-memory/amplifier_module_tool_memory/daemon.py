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


`GatewayClient` mirrors the subset of the store API that NativeMemoryStore
and the §8 harnesses need, sending the auth header.

Auth model (auth-tls-patterns skill): localhost "just works"; remote requires
the token. Token auto-generates to a 0600 file on first use.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from amplifier_module_tool_memory.embedder import DEFAULT_MODEL, FastEmbedEmbedder
from amplifier_module_tool_memory.store import NativeMemoryStore

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


def _dispatch_generic(
    store: Any, lock: threading.Lock, tool: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    """The original (§3.1 B1-additive) generic tool dispatch: write_cell ... batch.

    Shared by :func:`make_gateway` and :func:`make_daemon` (§5.4) so the memory
    daemon's new domain tools (remember/search/status/...) layer on top of this
    WITHOUT duplicating the low-level substrate dispatch. Returns ``None`` when
    *tool* is not one of the tools handled here -- callers fall through to their
    own (or raise ``ValueError`` for) unrecognized tools.
    """
    if True:
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
        return None


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
        result = _dispatch_generic(store, lock, tool, args)
        if result is None:
            raise ValueError(f"unknown tool: {tool}")
        return result

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
# Memory daemon (D3, §5 of docs/plans/2026-07-07-native-cutover-design.md)
#
# B1 lands the daemon's new capabilities IN this file (old module names,
# additive per §11's B1 scope -- the file/module rename to
# ``modules/tool-memory/.../daemon.py`` is a B3 mechanical move). The plain
# ``make_gateway``/``run_server``/``main`` surface above is UNCHANGED; the
# daemon is a superset built on top of it via ``_dispatch_generic``.
# ---------------------------------------------------------------------------


def daemon_version() -> str:
    """Resolve this package's installed version for the daemon's ``/health``
    and the client's version-mismatch check (§5.2) -- both sides call this
    SAME function so they can never disagree about what "current" means.
    Falls back to a dev sentinel when metadata is unavailable (editable
    installs without a build, or running straight from a source checkout
    that was never ``pip install``-ed).
    """
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("amplifier-module-tool-memory")
    except Exception:
        return "0.0.0-dev"


def default_memory_home() -> Path:
    """``~/.amplifier/memory`` (§5.1), overridable via ``AMPLIFIER_MEMORY_HOME``
    (tests/DTU isolation knob). This is a NEW home directory, distinct from
    the legacy vendor store's home directory -- this directory is owned
    exclusively by the native memory daemon.
    """
    override = os.environ.get("AMPLIFIER_MEMORY_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".amplifier" / "memory"


#: KG fact predicate marking a drawer filed while the embedder was not ready
#: (§4.3, KG-N3/KG-N7). A drawer carrying this predicate has NO vector --
#: query_vector alone will never surface it. The catch-up sweep below is the
#: ONLY consumer that clears it; without a sweep it queues forever and the
#: drawer is permanently unreachable once the embedder warms (the cold-start
#: data-loss bug this module exists to close).
_NEEDS_EMBEDDING_PREDICATE = "needs_embedding"


def _sweep_needs_embedding(
    mem_store: NativeMemoryStore,
    embedder: FastEmbedEmbedder,
    lock: threading.Lock,
) -> dict[str, int]:
    """Catch-up sweep (§4.3, KG-N7): embed every drawer still marked
    ``needs_embedding`` and invalidate the marker.

    Called from two places: the background watcher thread once the embedder
    transitions ready (:func:`_watch_embedder_and_sweep`), and cheaply at the
    top of ``remember``/``diary_write`` via :func:`_maybe_sweep` so a daemon
    that warmed while idle converges on its very next mutating op instead of
    waiting on the watcher's poll interval.

    Idempotent: once a marker fact is invalidated it no longer appears in
    ``query_facts``' currently-valid output, so re-running (from either
    caller, possibly racing) after full convergence is a single cheap query
    with zero work. Per-item failures (corrupt cell, transient embed error)
    are skipped and reported to stderr -- one bad drawer must never abort the
    sweep or crash the daemon (loud-but-graceful, same contract ``remember``
    itself uses for embedder failures).
    """
    s = mem_store.store
    pending = s.query_facts(predicate=_NEEDS_EMBEDDING_PREDICATE)  # type: ignore[attr-defined]
    swept = 0
    failed = 0
    if not pending.success:
        return {"swept": swept, "failed": failed}
    for fact in pending.output:
        ref = fact.subject
        try:
            content = s.regenerate(ref, record_access=False).payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            vector = embedder.embed(content)
            with lock:
                s.add_embedding(ref, vector)  # type: ignore[attr-defined]
                s.invalidate_fact(fact.subject, fact.predicate, fact.object)  # type: ignore[attr-defined]
            swept += 1
        except Exception as exc:  # loud but never crash the daemon (KG-N7)
            failed += 1
            sys.stderr.write(
                f"memory-daemon: needs_embedding sweep failed for {ref!r}: "
                f"{type(exc).__name__}: {exc}\n"
            )
    return {"swept": swept, "failed": failed}


def _maybe_sweep(
    mem_store: NativeMemoryStore,
    embedder: FastEmbedEmbedder | None,
    lock: threading.Lock,
) -> None:
    """Cheap opportunistic catch-up (KG-N7): if the embedder is ready and any
    ``needs_embedding`` facts remain, sweep them now.

    Costs exactly one ``query_facts`` call in the (common, steady-state) case
    where nothing is pending -- cheap enough to run unconditionally at the
    top of every ``remember``/``diary_write`` call rather than gating it
    behind a flag. This is what lets a daemon that warmed while idle converge
    on the very next mutating request instead of depending on the background
    watcher's poll timing.
    """
    if embedder is None or not embedder.ready:
        return
    s = mem_store.store
    pending = s.query_facts(predicate=_NEEDS_EMBEDDING_PREDICATE)  # type: ignore[attr-defined]
    if pending.success and pending.output:
        _sweep_needs_embedding(mem_store, embedder, lock)


def _watch_embedder_and_sweep(
    mem_store: NativeMemoryStore,
    embedder: FastEmbedEmbedder,
    lock: threading.Lock,
    *,
    poll_interval: float = 0.05,
) -> None:
    """Background thread target (§4.3, KG-N7): wait for the embedder's
    ready/failed transition, then run ONE catch-up sweep.

    Started by :func:`make_daemon` (daemon=True -- dies with the process,
    consistent with every other background thread in this module) whenever a
    non-``None`` embedder is configured. Three terminating conditions, all
    reached in bounded time:

    * ``embedder.ready`` is already True at construction (fast path, e.g. a
      warm cache or a test's always-ready fake) -- sweeps immediately (a
      no-op when nothing is pending) and returns.
    * ``embedder.failed`` is set (load failed, or never going to succeed) --
      nothing will ever become ready without a process restart, so there is
      nothing to wait for; returns without sweeping.
    * The embedder transitions ready mid-flight (the real first-run model
      download case this fix targets) -- sweeps once and returns.

    Deliberately does NOT loop forever polling for *new* needs_embedding
    facts after that: an embedder cannot un-ready itself, so one post-ready
    sweep converges everything pending at that moment. Anything filed after
    convergence lands with a real vector (the embedder is ready), and the
    rare filed-in-the-race-window case is covered by :func:`_maybe_sweep` on
    the next mutating op plus the search-hardening lexical union in
    :meth:`NativeMemoryStore.search`.
    """
    while True:
        if embedder.ready:
            _sweep_needs_embedding(mem_store, embedder, lock)
            return
        if embedder.failed:
            return
        time.sleep(poll_interval)


def _dispatch_domain(
    mem_store: NativeMemoryStore,
    embedder: FastEmbedEmbedder | None,
    lock: threading.Lock,
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """The §5.4 domain tools: remember/search/status/kg_*/traverse/diary_*/list_drawers.

    Layers on top of :func:`_dispatch_generic` (called first by
    :func:`make_daemon`'s dispatcher) via the ``NativeMemoryStore``-to-be seam
    (:class:`NativeMemoryStore`, still under its B1 name). Mutating ops
    take *lock*; embedding happens OUTSIDE the lock (§5.3: a slow embed must
    never block readers). Returns ``None`` for unrecognized tools so
    ``shutdown`` (handled by the caller, which owns the httpd reference) and
    truly-unknown tools fall through correctly.
    """
    if tool == "remember":
        _maybe_sweep(mem_store, embedder, lock)
        content = str(args.get("content", ""))
        vector: list[float] | None = None
        if embedder is not None and embedder.ready:
            try:
                vector = embedder.embed(content)
            except Exception:
                vector = (
                    None  # loud-but-graceful (KG-N3): fall through to needs_embedding
                )
        with lock:
            ref = mem_store.file(
                wing=str(args.get("wing", "general")),
                room=str(args.get("room", "notes")),
                content=content,
                source=str(args.get("source") or ""),
                category=args.get("category"),
                importance=args.get("importance"),
                embedding=vector,
            )
            if vector is None:
                mem_store.store.assert_fact(  # type: ignore[attr-defined]
                    ref,
                    "needs_embedding",
                    mem_store.store.write_cell(b"true"),  # type: ignore[attr-defined]
                )
        return {"ref": str(ref)}

    if tool == "search":
        query = str(args.get("query", ""))
        k = int(args.get("k", 5))
        wing = args.get("wing")
        room = args.get("room")
        vector = None
        degraded: str | None = None
        if embedder is not None and embedder.ready:
            try:
                vector = embedder.embed(query)
            except Exception:
                vector = None
                degraded = "lexical_only"
        else:
            degraded = "lexical_only"
        results = mem_store.search(vector, k, wing=wing, room=room, lexical_query=query)
        return {"results": results, "degraded": degraded}

    if tool == "status":
        st = mem_store.status()
        st["embedder"] = (
            {"ready": embedder.ready, "failed": embedder.failed}
            if embedder is not None
            else {"ready": False, "failed": "embedder disabled (--embedder-model none)"}
        )
        return st

    if tool == "kg_add":
        with lock:
            mem_store.assert_kg(
                str(args.get("subject", "")),
                str(args.get("predicate", "")),
                str(args.get("object", "")),
            )
        return {"ok": True}

    if tool == "kg_invalidate":
        with lock:
            mem_store.invalidate_kg(
                str(args.get("subject", "")),
                str(args.get("predicate", "")),
                str(args.get("object", "")),
            )
        return {"ok": True}

    if tool == "kg_query":
        facts = mem_store.query_kg(args.get("subject"), args.get("predicate"))
        return {"facts": [[s, p, o] for s, p, o in facts]}

    if tool == "kg_timeline":
        return {"entries": mem_store.kg_timeline(str(args.get("subject", "")))}

    if tool == "kg_stats":
        return mem_store.kg_stats()

    if tool == "traverse":
        start_ref = mem_store._anchor(str(args.get("start", "")))  # noqa: SLF001
        result = mem_store.store.query_graph(  # type: ignore[attr-defined]
            start_ref, int(args.get("max_hops", 2)), rel_type=args.get("rel_type")
        )
        return {"refs": list(result.output)}

    if tool == "diary_write":
        _maybe_sweep(mem_store, embedder, lock)
        with lock:
            ref = mem_store.file_diary(
                agent_name=str(args.get("agent_name", "")),
                entry=str(args.get("entry", "")),
                topic=str(args.get("topic", "general")),
            )
        return {"ref": str(ref)}

    if tool == "diary_read":
        entries = mem_store.read_diary(
            agent_name=str(args.get("agent_name", "")),
            last_n=int(args.get("last_n", 10)),
        )
        return {"entries": entries}

    if tool == "list_drawers":
        drawers = mem_store.list_drawers(
            wing=args.get("wing"),
            room=args.get("room"),
            limit=int(args.get("limit", 200)),
        )
        return {"drawers": drawers}

    return None


def make_daemon(
    store: Any,
    embedder: FastEmbedEmbedder | None,
    host: str,
    port: int,
    *,
    token: str,
    allow_localhost_bypass: bool = True,
    version: str | None = None,
    durable: bool = True,
    on_shutdown: Any = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the memory daemon over *store* (§5.4).

    Existing generic tools (write_cell ... batch) carry over verbatim via
    :func:`_dispatch_generic`. NEW: the §5.4 domain tools, ``shutdown``, and
    a ``/health`` payload extended with ``version``/``embedder``/``durable``.
    *on_shutdown*, when given, is called (in a background thread, AFTER the
    HTTP response is sent) when the ``shutdown`` tool fires -- ``run_daemon``
    uses it to close the store; ``daemon.json`` removal is the caller's job
    (it happens once ``serve_forever()`` returns).
    """
    lock = threading.Lock()
    mem_store = NativeMemoryStore(store=store)
    if embedder is not None:
        # Catch-up sweep watcher (§4.3, KG-N7): started at daemon build time
        # so it is exercised whenever tests call make_daemon() directly (the
        # same pattern the existing KG-N3 tests already use), not just via
        # run_daemon's production path.
        threading.Thread(
            target=_watch_embedder_and_sweep,
            args=(mem_store, embedder, lock),
            daemon=True,
        ).start()
    resolved_version = version if version is not None else daemon_version()
    httpd_holder: dict[str, ThreadingHTTPServer] = {}

    def _dispatch(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        generic = _dispatch_generic(store, lock, tool, args)
        if generic is not None:
            return generic
        if tool == "shutdown":

            def _do_shutdown() -> None:
                h = httpd_holder.get("httpd")
                if h is not None:
                    h.shutdown()
                if on_shutdown is not None:
                    on_shutdown()

            threading.Thread(target=_do_shutdown, daemon=True).start()
            return {"ok": True}
        domain = _dispatch_domain(mem_store, embedder, lock, tool, args)
        if domain is not None:
            return domain
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
                self._send(
                    200,
                    {
                        "ok": True,
                        "service": "memory-daemon",
                        "version": resolved_version,
                        "embedder": (
                            {"ready": embedder.ready, "failed": embedder.failed}
                            if embedder is not None
                            else {"ready": False, "failed": "embedder disabled"}
                        ),
                        "durable": durable,
                    },
                )
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

    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd_holder["httpd"] = httpd
    return httpd


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

    def write_batch(self) -> GatewayWriteBatch:
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

    def __init__(self, client: GatewayClient) -> None:
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

    def relate(self, from_ref: str, to_ref: str, rel_type: str) -> GatewayWriteBatch:
        self._ops.append(
            {
                "op": "relate",
                "from_ref": from_ref,
                "to_ref": to_ref,
                "rel_type": rel_type,
            }
        )
        return self

    def assert_fact(
        self, subject: str, predicate: str, object: str
    ) -> GatewayWriteBatch:  # noqa: A002
        self._ops.append(
            {
                "op": "assert_fact",
                "subject": subject,
                "predicate": predicate,
                "object": object,
            }
        )
        return self

    def scope(self, cell_ref: str, scope_ref: str) -> GatewayWriteBatch:
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
# Daemon lifecycle (§5.1, §5.2, §5.6) — daemon.json discovery file + durability gate
# ---------------------------------------------------------------------------


def _write_daemon_json(home: Path, info: dict[str, Any]) -> None:
    """Atomically write ``daemon.json`` (tmp + os.replace, §5.1)."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = home / "daemon.json"
    tmp = home / "daemon.json.tmp"
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, target)


def run_daemon(
    *,
    home: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    ephemeral: bool = False,
    embedder_model: str = DEFAULT_MODEL,
    token_path: str | Path | None = None,
) -> int:
    """Run the memory daemon: open the store, warm the embedder, serve (§5.2, §5.6).

    Opens a DURABLE store at ``home/store.log`` (REQUIRES the amplifier-data
    Rust kernel, D10) unless *ephemeral* is set, in which case an in-memory
    store is used and ``/health`` reports ``durable: false`` -- tests/DTU
    smoke ONLY, never production (a durability gate this function enforces
    itself: it refuses to start a non-ephemeral store without the kernel).

    Starts the embedder's warm-load on a background thread (non-blocking --
    the daemon serves immediately; §4.3), writes ``daemon.json`` atomically
    once listening, then blocks in ``serve_forever()``. ``SIGTERM`` and the
    ``shutdown`` dispatch tool both trigger a graceful stop: the httpd loop
    exits, the store is closed, and ``daemon.json`` is removed before this
    function returns.
    """
    resolved_home = home if home is not None else default_memory_home()
    resolved_home.mkdir(mode=0o700, parents=True, exist_ok=True)

    durable = not ephemeral
    if durable:
        from amplifier_data import RUST_AVAILABLE

        if not RUST_AVAILABLE:
            sys.stderr.write(
                "memory-daemon: durable storage requires the compiled amplifier-data "
                "Rust kernel (RUST_AVAILABLE is False). Install the amplifier-data git "
                "pin (`pip install '.[substrate]'`; builds the kernel via maturin -- a "
                "Rust toolchain is the prerequisite) and retry, or pass --ephemeral for "
                "a non-durable test/DTU-only store (never production; D10).\n"
            )
            return 1

    from amplifier_data import AmplifierStore

    store = (
        AmplifierStore(path=str(resolved_home / "store.log"))
        if durable
        else AmplifierStore(record_access=False)
    )

    embedder: FastEmbedEmbedder | None = None
    if embedder_model != "none":
        started_embedder = FastEmbedEmbedder(embedder_model)
        embedder = started_embedder
        threading.Thread(target=started_embedder.warm, daemon=True).start()

    resolved_token_path = Path(token_path) if token_path else (resolved_home / "token")
    token = ensure_token(resolved_token_path)
    version = daemon_version()

    def _close_store() -> None:
        store.close()

    httpd = make_daemon(
        store,
        embedder,
        host,
        port,
        token=token,
        version=version,
        durable=durable,
        on_shutdown=_close_store,
    )
    chosen_port = httpd.server_address[1]

    daemon_json_path = resolved_home / "daemon.json"
    info = {
        "url": f"http://{host}:{chosen_port}",
        "host": host,
        "port": chosen_port,
        "pid": os.getpid(),
        "version": version,
        "token_file": str(resolved_token_path),
        "started_at": datetime.now(UTC).isoformat(),
        "durable": durable,
    }
    _write_daemon_json(resolved_home, info)

    def _handle_sigterm(signum: int, frame: Any) -> None:  # noqa: ARG001
        # Signal handlers run in the main thread, which is the SAME thread
        # blocked in serve_forever() below -- calling httpd.shutdown()
        # directly here would deadlock (it waits for the serve_forever loop
        # to notice, which can't happen until this handler returns). Do it
        # from a fresh thread instead, mirroring the "shutdown" tool's path.
        threading.Thread(
            target=lambda: (httpd.shutdown(), _close_store()), daemon=True
        ).start()

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass  # not called from the main thread (e.g. some test harnesses) -- skip

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
        daemon_json_path.unlink(missing_ok=True)
    return 0


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
    parser = argparse.ArgumentParser(prog="memory-daemon")
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
    # --daemon (§5, additive): run as the upgraded memory daemon (embedder +
    # §5.4 domain tools + daemon.json lifecycle) instead of the plain gateway
    # above. Absent, every flag/behavior above is UNCHANGED (B1: no existing
    # behavior changes). ``ensure_daemon()`` in client.py spawns via
    # ``python -m ...amplifier_data_gateway --daemon`` rather than a dedicated
    # console script (the console-script rename is a B3 concern, §3.1).
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="run as the memory daemon instead of the plain gateway",
    )
    parser.add_argument(
        "--home",
        default=None,
        help=(
            "--daemon mode only: memory home dir "
            "(default ~/.amplifier/memory or $AMPLIFIER_MEMORY_HOME)"
        ),
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="--daemon mode only: in-memory store -- tests/DTU only, never production (D10)",
    )
    parser.add_argument(
        "--embedder-model",
        default=DEFAULT_MODEL,
        help="--daemon mode only: fastembed model name, or 'none' for lexical-only by policy",
    )
    args = parser.parse_args(argv)

    if args.daemon:
        home = Path(args.home).expanduser() if args.home else default_memory_home()
        return run_daemon(
            home=home,
            host=args.host,
            port=args.port,
            ephemeral=args.ephemeral,
            embedder_model=args.embedder_model,
            token_path=args.token_file,
        )

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
