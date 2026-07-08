"""MemoryClient \u2014 the ONE seam every hook/tool/pipeline op will use in B2
(\u00a73.2 of docs/plans/2026-07-07-native-cutover-design.md).

Absorbs :class:`~amplifier_module_tool_memory.daemon.GatewayClient`
wholesale (write_cell/scope/assert_fact/invalidate_fact/regenerate/
graph_neighbors/query_facts/add_embedding/query_vector/write_batch) via
subclassing, and adds domain calls mirroring the daemon's \u00a75.4 dispatch
tools: remember, search, status, kg_query, kg_timeline, kg_stats, traverse,
diary_write, diary_read, list_drawers, shutdown. Stdlib-only (urllib) -- no
amplifier-data, no fastembed import in session processes; only the daemon
subprocess needs those.

``ensure_daemon()`` implements \u00a75.2's discovery -> health-check -> spawn-race
-> recovery protocol. B1 is purely additive: nothing here is wired into
``MemoryTool`` or the hooks yet (that is B2); this module is exercised
directly by tests and by future B2 call sites.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .daemon import (
    GatewayClient,
    daemon_version,
    default_memory_home,
)

__all__ = ["MemoryClient", "ensure_daemon"]

# Per-process guard: emit memory:daemon_unavailable at most once per process
# (\u00a75.2 step 3), regardless of how many times ensure_daemon() is retried.
_unavailable_emitted = False
_unavailable_lock = threading.Lock()


def _emit(event: str, *, ok: bool, data: dict[str, Any]) -> None:
    """Best-effort JSONL event emission -- never raises, never blocks a caller."""
    try:
        from .event_emitter import emit_event

        emit_event("memory-daemon", event, ok=ok, data=data)
    except Exception:
        pass


class MemoryClient(GatewayClient):
    """``GatewayClient`` + the \u00a75.4 domain calls, over the SAME authed transport."""

    def remember(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> str:
        out = self._call(
            "remember",
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
            },
        )
        return str(out["ref"])

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        wing: str | None = None,
        room: str | None = None,
    ) -> dict[str, Any]:
        """``{results: [...], degraded: null|"lexical_only"}`` (\u00a75.4)."""
        return self._call(
            "search", {"query": query, "k": k, "wing": wing, "room": room}
        )

    def status(self) -> dict[str, Any]:
        return self._call("status", {})

    def kg_query(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[tuple[str, str, str]]:
        out = self._call("kg_query", {"subject": subject, "predicate": predicate})
        return [(f[0], f[1], f[2]) for f in out["facts"]]

    def kg_add(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        """Anchor-resolved KG assert (B2 parity gap-fill: the \u00a75.4 dispatch-tool
        table only lists kg_query/kg_timeline/kg_stats, but MemoryTool's ``kg``
        operation supports add/invalidate and must keep working natively.
        Mirrors ``NativeMemoryStore.assert_kg`` via the daemon's
        ``kg_add`` dispatch tool, added alongside this method."""
        self._call(
            "kg_add", {"subject": subject, "predicate": predicate, "object": object}
        )

    def kg_invalidate(self, subject: str, predicate: str, object: str) -> None:  # noqa: A002
        """Anchor-resolved KG invalidate -- see :meth:`kg_add`."""
        self._call(
            "kg_invalidate",
            {"subject": subject, "predicate": predicate, "object": object},
        )

    def kg_timeline(self, subject: str) -> list[dict[str, Any]]:
        return self._call("kg_timeline", {"subject": subject})["entries"]

    def kg_stats(self) -> dict[str, int]:
        return self._call("kg_stats", {})

    def traverse(
        self, start: str, max_hops: int = 2, rel_type: str | None = None
    ) -> list[str]:
        out = self._call(
            "traverse", {"start": start, "max_hops": max_hops, "rel_type": rel_type}
        )
        return list(out["refs"])

    def diary_write(
        self, *, agent_name: str, entry: str, topic: str = "general"
    ) -> str:
        out = self._call(
            "diary_write", {"agent_name": agent_name, "entry": entry, "topic": topic}
        )
        return str(out["ref"])

    def diary_read(self, *, agent_name: str, last_n: int = 10) -> list[dict[str, Any]]:
        out = self._call("diary_read", {"agent_name": agent_name, "last_n": last_n})
        return list(out["entries"])

    def list_drawers(
        self, *, wing: str | None = None, room: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        out = self._call("list_drawers", {"wing": wing, "room": room, "limit": limit})
        return list(out["drawers"])

    def shutdown(self) -> None:
        """Ask the daemon to shut down gracefully. Never raises (best-effort)."""
        with contextlib.suppress(Exception):
            self._call("shutdown", {})

    def health(self, *, timeout: float = 5.0) -> dict[str, Any] | None:
        """``GET /health`` -- ``None`` on any transport failure (never raises)."""
        return _health(self.base_url, timeout=timeout)


# ---------------------------------------------------------------------------
# ensure_daemon() -- discovery, spawn race, recovery (\u00a75.2)
# ---------------------------------------------------------------------------


def _read_daemon_json(home: Path) -> dict[str, Any] | None:
    path = home / "daemon.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_token(info: dict[str, Any]) -> str | None:
    token_file = info.get("token_file")
    if not token_file:
        return None
    p = Path(str(token_file))
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _health(url: str, *, timeout: float) -> dict[str, Any] | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else
    except OSError:
        return False
    return True


def _client_from_info(info: dict[str, Any]) -> MemoryClient:
    return MemoryClient(str(info.get("url", "")), _read_token(info))


def _discover(home: Path, *, allow_recover: bool = True) -> MemoryClient | None:
    """\u00a75.2 step 1: read daemon.json, health-check, handle mismatch/staleness."""
    info = _read_daemon_json(home)
    if info is None:
        return None  # nothing to discover -- caller proceeds to spawn

    url = str(info.get("url", ""))
    pid = info.get("pid")
    hc = _health(url, timeout=1.0)

    if hc is not None:
        if hc.get("version") == daemon_version():
            return _client_from_info(info)
        # Version mismatch (\u00a75.2 step 1c, the upgrade path): ask the old
        # daemon to shut down, wait up to 5s, then fall through to spawn.
        with contextlib.suppress(Exception):
            _client_from_info(info).shutdown()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _health(url, timeout=0.5) is None:
                break
            time.sleep(0.2)
        return None

    # Unhealthy. Stale pid (process gone) -> immediately treat as gone (KG-N6).
    if pid is not None and not _pid_alive(int(pid)):
        return None

    if not allow_recover:
        return None

    # pid alive but health keeps failing -- a wedged daemon (\u00a75.2 step 1d).
    for _ in range(3):
        time.sleep(1.0)
        recovered = _health(url, timeout=1.0)
        if recovered is not None:
            return _discover(home, allow_recover=False)  # re-evaluate from the top
    if pid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(int(pid), signal.SIGTERM)
        time.sleep(2.0)
    return None


def _poll_until_healthy(home: Path, *, timeout: float) -> MemoryClient | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _read_daemon_json(home)
        if info is not None:
            hc = _health(str(info.get("url", "")), timeout=1.0)
            if hc is not None and hc.get("ok"):
                return _client_from_info(info)
        time.sleep(0.2)
    return None


def _spawn_daemon_process(home: Path) -> None:
    """Launch the daemon subprocess via ``python -m`` (\u00a75.2 step 2, winner path).

    Uses ``-m`` rather than a dedicated console script so ``ensure_daemon``
    works in any environment where this package is importable -- no separate
    entry-point registration required. The console-script rename (\u00a73.1,
    ``memory-daemon``) is a B3 concern; ``main()``'s ``--daemon`` flag (added
    in this same B1 pass) is the stable target either way.
    """
    log_path = home / "daemon.log"
    with open(log_path, "a", encoding="utf-8") as log_fh:
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "amplifier_module_tool_memory.daemon",
                "--daemon",
                "--home",
                str(home),
            ],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )


def _spawn_and_wait(home: Path, *, retry: bool = True) -> MemoryClient | None:
    """\u00a75.2 step 2: O_CREAT|O_EXCL spawn-lock race, winner spawns, loser polls."""
    lock_path = home / "daemon.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0.0
        if age > 30.0 and retry:
            # Stale lock (no healthy daemon behind it, \u00a75.2 step 2): clear and
            # retry exactly once.
            with contextlib.suppress(OSError):
                lock_path.unlink()
            return _spawn_and_wait(home, retry=False)
        # Loser: the winner is starting it -- poll for readiness.
        return _poll_until_healthy(home, timeout=10.0)

    try:
        os.close(fd)
        _spawn_daemon_process(home)
        client = _poll_until_healthy(home, timeout=10.0)
        if client is not None:
            _emit("daemon_spawned", ok=True, data={"home": str(home)})
        return client
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def ensure_daemon(home: Path | str | None = None) -> MemoryClient | None:
    """Discovery -> health-check -> (re)spawn per \u00a75.2. NEVER raises.

    Returns ``None`` only when spawning is genuinely impossible (e.g. the
    daemon subprocess never becomes healthy within its startup budget).
    Callers MUST degrade loudly per their own contract (\u00a75.7) rather than
    treating a ``None`` return as fatal -- this function itself emits
    ``memory:daemon_unavailable`` at most once per process on that path.
    """
    global _unavailable_emitted

    resolved_home = Path(home).expanduser() if home is not None else default_memory_home()
    try:
        resolved_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        client = _discover(resolved_home)
        if client is not None:
            return client
        client = _spawn_and_wait(resolved_home)
        if client is not None:
            return client
    except Exception as exc:  # noqa: BLE001 -- NEVER-raises contract; observed, not silent
        _emit(
            "daemon_ensure_error",
            ok=False,
            data={"home": str(resolved_home), "error": repr(exc)},
        )

    with _unavailable_lock:
        already = _unavailable_emitted
        _unavailable_emitted = True
    if not already:
        _emit("daemon_unavailable", ok=False, data={"home": str(resolved_home)})
    return None
