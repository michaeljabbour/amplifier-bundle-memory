"""
amplifier-module-hooks-mempalace-capture

Amplifier hook that fires on tool:post events and automatically files
verbatim tool outputs as MemPalace drawers. Wing is auto-detected from
the git remote or cwd project name; room is derived from the tool name
and active task context.

This module consolidates two previously separate hooks:
  - hooks-memory-capture (lightweight heuristic category detection)
  - hooks-mempalace-capture (verbatim palace filing)

The category detection from hooks-memory-capture is now built in:
outputs are classified into decision/architecture/blocker/pattern/etc.
before filing, and the room name is enriched with the category.

Latency model
-------------
Per amplifier first principles, hooks must not block the kernel's event
dispatch.  The on-event handler does only cheap work: heuristic gate,
category detection, build a job envelope, spool the payload to disk,
emit ``capture_queued``, and ``put_nowait`` onto an in-process queue.
A daemon thread drains the queue and performs the slow work
(``git remote get-url`` for wing detection, ``mempalace mcp --call`` for
the drawer write) entirely off the hot path.

Durability
----------
We rely on amplifier's native session re-hydration as the recovery
mechanism.  Each queued capture is spooled to ``~/.mempalace/spool/{sid}/``
and an entry recorded in the session event log.  The drain worker deletes
the spool file on ``drawer_filed`` or ``capture_failed``.  On ``mount``
the module sweeps the spool dir for the resolved session id and re-enqueues
anything orphaned by a prior crash — when amplifier resumes the session,
the spool dir is restored alongside the event log, and we pick up where
we left off.

Credits: MemPalace (github.com/MemPalace/mempalace).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from amplifier_core import HookResult  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests)
    class HookResult:  # type: ignore
        def __init__(self, *, action: str = "continue", **kwargs: Any) -> None:
            self.action = action
            for k, v in kwargs.items():
                setattr(self, k, v)


try:
    from amplifier_module_tool_mempalace.event_emitter import (
        emit_event,
        read_events,
        truncate_preview,
    )
    from amplifier_module_tool_mempalace.event_emitter import (
        _resolve_session_id as _emitter_resolve_session_id,
    )
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass

    def read_events(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # type: ignore[misc]
        return []

    def truncate_preview(text: Any) -> Any:  # type: ignore[misc]
        if text is None:
            return None
        return text[:97] + "..." if len(text) > 100 else text

    def _emitter_resolve_session_id(session_id: str | None = None) -> str:  # type: ignore[misc]
        if session_id is not None:
            return session_id
        return os.environ.get("AMPLIFIER_SESSION_ID") or "unknown"


try:
    from amplifier_module_tool_mempalace.coordinator_bridge import (
        NOOP_SYNC_BRIDGE,
        SyncBridge,
        make_sync_bridge,
        register_events,
    )
except ImportError:
    if not TYPE_CHECKING:
        SyncBridge = Any  # runtime fallback; type comes from the try-import branch

    def NOOP_SYNC_BRIDGE(event: str, payload: Any) -> None:  # type: ignore[misc]
        pass

    def make_sync_bridge(coordinator: Any) -> Any:  # type: ignore[misc]
        return NOOP_SYNC_BRIDGE

    def register_events(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


try:
    from amplifier_module_tool_mempalace.manifest import (
        load_manifest as _load_manifest,
    )
except ImportError:
    _load_manifest = None  # type: ignore[assignment]

try:
    from amplifier_module_tool_mempalace.scripts.memory_store import (
        AmplifierDataMemoryStore as _AmplifierDataMemoryStore,
    )
except ImportError:
    _AmplifierDataMemoryStore = None  # type: ignore[assignment,misc]

# Best-effort amplifier-data shadow store (the "running shadow"): set by mount()
# from the shadow_gateway config. None disables shadowing. The client side is
# pure urllib (GatewayClient) — the capture process does NOT need amplifier-data
# installed; only the gateway service process does.
_SHADOW_STORE: Any = None


def _detect_wing(cwd: str | None = None) -> str:
    """Detect the active project wing from git remote or directory name."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or os.getcwd(),
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract repo name from URL: github.com/user/repo-name → repo-name
            name = url.rstrip("/").split("/")[-1].replace(".git", "")
            return f"wing_{name}" if name else "wing_general"
    except Exception:
        pass
    # Fall back to directory name
    cwd_path = Path(cwd or os.getcwd())
    return f"wing_{cwd_path.name}"


def _detect_room(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Derive a room name from the tool name and its input."""
    # Map common tool names to meaningful room names
    room_map = {
        "bash": "shell-commands",
        "computer": "computer-use",
        "read_file": "file-reads",
        "write_file": "file-writes",
        "edit_file": "file-edits",
        "search": "search-results",
        "web_search": "search-results",
        "browser": "browser-actions",
        "delegate": "delegated-tasks",
    }
    base = room_map.get(tool_name, tool_name.replace("_", "-"))

    # Refine room from file path if available
    for key in ("path", "file_path", "filename"):
        if key in tool_input:
            p = Path(str(tool_input[key]))
            if p.suffix in (".py", ".ts", ".tsx", ".js", ".rs", ".go"):
                return f"{p.stem}-{p.suffix.lstrip('.')}"
            if p.name:
                return p.stem[:40]

    return base


def _is_palace_worthy(tool_name: str, output: str) -> bool:
    """Heuristic: is this output worth filing in the palace?"""
    if not output or len(output) < 50:
        return False
    # Skip noisy or binary-like outputs
    skip_tools = {"mempalace_status", "mempalace_reconnect", "mempalace_hook_settings"}
    if tool_name in skip_tools:
        return False
    # Skip very long outputs (>8KB) — too noisy for verbatim storage
    if len(output) > 8192:
        return False
    return True


def _skip_reason(tool_name: str, output: str) -> str:
    """Return the reason why an output is not palace-worthy."""
    if not output or len(output) < 50:
        return "too_short"
    skip_tools = {"mempalace_status", "mempalace_reconnect", "mempalace_hook_settings"}
    if tool_name in skip_tools:
        return "skip_tool"
    if len(output) > 8192:
        return "too_long"
    return "too_short"


def _mcp_add_drawer(wing: str, room: str, content: str, source: str) -> None:
    """File a verbatim drawer via the MemPalace CLI."""
    payload = json.dumps(
        {
            "tool": "mempalace_add_drawer",
            "arguments": {
                "wing": wing,
                "room": room,
                "content": content,
                "source_file": source,
                "added_by": "hooks-mempalace-capture",
            },
        }
    )
    subprocess.run(
        ["mempalace", "mcp", "--call", payload],
        capture_output=True,
        text=True,
        timeout=15,
    )


# Category keyword signals (absorbed from hooks-memory-capture)
_CATEGORY_SIGNALS: dict[str, list[str]] = {
    "decision": ["decided", "decision", "we will", "going with", "chosen", "agreed"],
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "structure",
        "component",
        "module",
    ],
    "blocker": ["blocked", "blocking", "cannot", "failed", "error", "issue", "problem"],
    "resolved_blocker": [
        "fixed",
        "resolved",
        "workaround",
        "solution found",
        "now works",
    ],
    "dependency": ["depends on", "requires", "dependency", "import", "package"],
    "pattern": ["pattern", "convention", "always", "never", "best practice", "rule"],
    "lesson_learned": [
        "learned",
        "lesson",
        "turns out",
        "discovered",
        "realized",
        "note:",
    ],
}


def _detect_category(
    text: str, signals: dict[str, list[str]] | None = None
) -> str | None:
    """Heuristically detect a memory category from text content.

    ``signals`` maps category id -> list of lowercase keyword seeds. When None,
    the legacy hardcoded ``_CATEGORY_SIGNALS`` table is used so callers that do
    not supply a manifest behave exactly as before. First category (in
    declaration order) with any matching seed wins.
    """
    table = signals if signals is not None else _CATEGORY_SIGNALS
    lower = text.lower()
    for category, seeds in table.items():
        if any(seed in lower for seed in seeds):
            return category
    return None


# ---------------------------------------------------------------------------
# Queue + drain (the de-blocking machinery)
# ---------------------------------------------------------------------------
#
# The on-event handler does only cheap work and a put_nowait on _QUEUE.
# A single daemon thread (_DRAIN_THREAD) does the slow work — git subprocess
# for wing detection and the mempalace mcp subprocess for the drawer write.
# Module-level state is appropriate here: hooks are per-process and we want
# exactly one drain thread per process regardless of how many sessions run.

_QUEUE_MAXSIZE = 1024  # bounded — drop with a recorded event on overflow
_QUEUE: queue.Queue["_CaptureJob"] | None = None
_DRAIN_THREAD: threading.Thread | None = None
_DRAIN_LOCK = threading.Lock()
# Bridge for drain thread → coordinator forwarding. Set by mount().
_DRAIN_BRIDGE: SyncBridge = NOOP_SYNC_BRIDGE


@dataclass(frozen=True)
class _CaptureJob:
    """One unit of deferred work for the drain thread."""

    capture_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: str
    source: str
    category: str | None
    session_id: str | None
    enqueued_at: str
    auto_wing: bool = True
    auto_room: bool = True
    config_wing: str = "wing_general"
    config_room: str = "general"
    emit_events: bool = True
    spool_path: str | None = field(default=None, compare=False)


def _spool_dir_for(session_id: str | None) -> Path | None:
    """Return ~/.mempalace/spool/{sid}/ if mempalace is initialised, else None.

    Mirrors the event emitter's behaviour: if ~/.mempalace/ does not exist
    we never create it, and the whole module silently no-ops.
    """
    base = Path.home() / ".mempalace"
    if not base.exists():
        return None
    sid = _emitter_resolve_session_id(session_id)
    return base / "spool" / sid


def _spool_write(job_dir: Path, capture_id: str, payload: dict[str, Any]) -> Path:
    """Atomically write a job payload to the spool directory."""
    job_dir.mkdir(parents=True, exist_ok=True)
    final = job_dir / f"{capture_id}.json"
    tmp = job_dir / f"{capture_id}.json.tmp"
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, final)  # atomic on POSIX
    return final


def _spool_delete(spool_path: str | Path | None) -> None:
    """Remove a spool file. Idempotent and exception-safe."""
    if not spool_path:
        return
    try:
        p = Path(spool_path)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _ensure_drain_thread() -> queue.Queue["_CaptureJob"]:
    """Lazily start the drain thread on first use. Idempotent + thread-safe."""
    global _QUEUE, _DRAIN_THREAD
    with _DRAIN_LOCK:
        if _QUEUE is None:
            _QUEUE = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        if _DRAIN_THREAD is None or not _DRAIN_THREAD.is_alive():
            _DRAIN_THREAD = threading.Thread(
                target=_drain_loop,
                name="mempalace-capture-drain",
                daemon=True,
            )
            _DRAIN_THREAD.start()
        return _QUEUE


def _drain_loop() -> None:
    """Background worker: pull jobs and do the slow work.

    The worker MUST NOT die.  Any exception inside one job is swallowed and
    recorded as ``capture_failed``; the loop continues forever.
    """
    assert _QUEUE is not None
    while True:
        try:
            job = _QUEUE.get()
        except Exception:
            continue
        try:
            _process_job(job)
        except Exception:
            # Last-resort guard: never let the worker die.
            try:
                if job.emit_events:
                    emit_event(
                        "mempalace-capture",
                        "capture_failed",
                        ok=False,
                        preview=truncate_preview(job.tool_output),
                        data={
                            "capture_id": job.capture_id,
                            "reason": "worker_exception",
                        },
                        session_id=job.session_id,
                    )
                    try:
                        _DRAIN_BRIDGE(
                            "memory-mempalace:capture_failed",
                            {
                                "capture_id": job.capture_id,
                                "reason": "worker_exception",
                            },
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            _spool_delete(job.spool_path)
        finally:
            try:
                _QUEUE.task_done()
            except Exception:
                pass


def _shadow_job(job: _CaptureJob, wing: str, room: str) -> None:
    """Best-effort shadow the filed drawer to amplifier-data via the gateway.

    The palace is the source of truth; the shadow NEVER blocks or fails capture.
    Disabled unless mount() configured ``_SHADOW_STORE`` from ``shadow_gateway``.
    """
    store = _SHADOW_STORE
    if store is None:
        return
    try:
        store.file(
            wing=wing,
            room=room,
            content=job.tool_output,
            source=job.source,
            category=job.category,
        )
        if job.emit_events:
            emit_event(
                "mempalace-capture",
                "shadow_filed",
                ok=True,
                preview=truncate_preview(job.tool_output),
                data={"capture_id": job.capture_id, "wing": wing, "room": room},
                session_id=job.session_id,
            )
    except Exception as exc:  # shadow must never break the source-of-truth write
        if job.emit_events:
            emit_event(
                "mempalace-capture",
                "shadow_failed",
                ok=False,
                data={"capture_id": job.capture_id, "reason": type(exc).__name__},
                session_id=job.session_id,
            )


def _process_job(job: _CaptureJob) -> None:
    """Do one capture's slow work: detect wing, file drawer, emit completion."""
    wing = _detect_wing() if job.auto_wing else job.config_wing
    base_room = (
        _detect_room(job.tool_name, job.tool_input)
        if job.auto_room
        else job.config_room
    )
    room = f"{base_room}-{job.category}" if job.category else base_room

    try:
        _mcp_add_drawer(wing, room, job.tool_output, job.source)
        if job.emit_events:
            emit_event(
                "mempalace-capture",
                "drawer_filed",
                ok=True,
                preview=truncate_preview(job.tool_output),
                data={
                    "capture_id": job.capture_id,
                    "wing": wing,
                    "room": room,
                    "category": job.category,
                    "content_bytes": len(job.tool_output.encode("utf-8")),
                    "source": job.source,
                },
                session_id=job.session_id,
            )
            try:
                _DRAIN_BRIDGE(
                    "memory-mempalace:drawer_filed",
                    {
                        "capture_id": job.capture_id,
                        "wing": wing,
                        "room": room,
                        "category": job.category,
                        "content_bytes": len(job.tool_output.encode("utf-8")),
                        "source": job.source,
                        "ok": True,
                        "preview": truncate_preview(job.tool_output),
                    },
                )
            except Exception:
                pass
        _shadow_job(job, wing, room)
        _spool_delete(job.spool_path)
    except Exception:
        if job.emit_events:
            emit_event(
                "mempalace-capture",
                "capture_failed",
                ok=False,
                preview=truncate_preview(job.tool_output),
                data={"capture_id": job.capture_id, "reason": "mcp_error"},
                session_id=job.session_id,
            )
            try:
                _DRAIN_BRIDGE(
                    "memory-mempalace:capture_failed",
                    {
                        "capture_id": job.capture_id,
                        "reason": "mcp_error",
                    },
                )
            except Exception:
                pass
        # Leave the spool entry in place so a future resume can retry.


def _replay_orphans(session_id: str | None, *, emit_events: bool) -> int:
    """Re-enqueue spool entries with no completion event.

    Called from ``mount``.  Uses amplifier's native session re-hydration:
    when a session resumes, the event log AND the spool dir are restored
    intact, so we can detect captures that were queued but never finished
    and put them back on the work queue.
    """
    spool_dir = _spool_dir_for(session_id)
    if spool_dir is None or not spool_dir.exists():
        return 0

    completed_ids: set[str] = set()
    for ev_name in ("drawer_filed", "capture_failed"):
        for ev in read_events(
            session_id,
            hook_filter="mempalace-capture",
            event_filter=ev_name,
            limit=100_000,
        ):
            cid = (ev.get("data") or {}).get("capture_id")
            if cid:
                completed_ids.add(cid)

    work_queue = _ensure_drain_thread()
    replayed = 0
    for spool_file in spool_dir.glob("*.json"):
        capture_id = spool_file.stem
        if capture_id in completed_ids:
            _spool_delete(spool_file)
            continue
        try:
            payload = json.loads(spool_file.read_text(encoding="utf-8"))
            payload["spool_path"] = str(spool_file)
            job = _CaptureJob(**payload)
        except Exception:
            # Corrupt spool file — drop it silently.
            _spool_delete(spool_file)
            continue
        try:
            work_queue.put_nowait(job)
            replayed += 1
        except queue.Full:
            # We'll get another chance on the next mount.
            break

    if replayed and emit_events:
        emit_event(
            "mempalace-capture",
            "replay_enqueued",
            ok=True,
            data={"count": replayed},
            session_id=session_id,
        )
    return replayed


class MempalaceCaptureHook:
    name = "hooks-mempalace-capture"
    events = ["tool:post"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        bridge_emit: SyncBridge | None = None,
    ) -> None:
        self.config = config or {}
        self.auto_wing: bool = self.config.get("auto_wing", True)
        self.auto_room: bool = self.config.get("auto_room", True)
        self.silent: bool = self.config.get("silent", True)
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        # Categories to capture (empty list = capture all palace-worthy content)
        self.categories: list[str] = self.config.get("categories", [])
        # Load category signals from the capture manifest (the "knowable list").
        # Resolution order is handled by load_manifest; falls back to the legacy
        # hardcoded table when the manifest module is unavailable or the file is
        # missing/malformed, so behavior is unchanged when no manifest exists.
        manifest_path = self.config.get("manifest_path")
        signals: dict[str, list[str]] = dict(_CATEGORY_SIGNALS)
        if _load_manifest is not None:
            try:
                signals = _load_manifest(config_path=manifest_path).category_signals()
            except Exception:
                signals = dict(_CATEGORY_SIGNALS)
        self._signals: dict[str, list[str]] = signals
        # Coordinator bridge — no-op default keeps the drain thread safe in tests
        self._bridge_emit: SyncBridge = bridge_emit or NOOP_SYNC_BRIDGE

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        """Hot-path handler.

        Does only cheap, deterministic work: heuristic gate, category
        detection, spool the payload, emit ``capture_queued``, enqueue.
        Returns immediately.  All slow work — git subprocess, mempalace
        subprocess — happens in the drain thread.
        """
        tool_name: str = data.get("tool_name", "unknown")
        tool_input: dict[str, Any] = data.get("tool_input", {}) or {}
        tool_output: str = str(data.get("tool_output", ""))
        sid = data.get("session_id")

        if not _is_palace_worthy(tool_name, tool_output):
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output) if tool_output else None,
                    data={"reason": _skip_reason(tool_name, tool_output)},
                    session_id=sid,
                )
            return HookResult(action="continue")

        category = _detect_category(tool_output, self._signals)
        if self.categories and category not in self.categories:
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output),
                    data={"reason": "category_filtered"},
                    session_id=sid,
                )
            return HookResult(action="continue")

        capture_id = uuid.uuid4().hex
        source = str(tool_input.get("path", tool_input.get("file_path", tool_name)))
        job = _CaptureJob(
            capture_id=capture_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            source=source,
            category=category,
            session_id=sid,
            enqueued_at=datetime.now(UTC).isoformat(),
            auto_wing=self.auto_wing,
            auto_room=self.auto_room,
            config_wing=self.config.get("wing", "wing_general"),
            config_room=self.config.get("room", "general"),
            emit_events=self.emit_events,
        )

        # Spool to disk so a crash leaves the work recoverable on next mount.
        spool_dir = _spool_dir_for(sid)
        spool_path: Path | None = None
        if spool_dir is not None:
            try:
                payload = asdict(job)
                payload.pop("spool_path", None)
                spool_path = _spool_write(spool_dir, capture_id, payload)
            except Exception:
                spool_path = None  # spool failure is non-fatal — we still queue
        if spool_path is not None:
            job = _CaptureJob(**{**asdict(job), "spool_path": str(spool_path)})

        work_queue = _ensure_drain_thread()
        try:
            work_queue.put_nowait(job)
        except queue.Full:
            # Backpressure: the worker is behind. Don't block the kernel.
            # Drop with a visible event; the spool entry remains so a future
            # mount-time replay can pick it up.
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "capture_overflowed",
                    ok=False,
                    preview=truncate_preview(tool_output),
                    data={
                        "capture_id": capture_id,
                        "queue_maxsize": _QUEUE_MAXSIZE,
                    },
                    session_id=sid,
                )
            return HookResult(action="continue")

        if self.emit_events:
            emit_event(
                "mempalace-capture",
                "capture_queued",
                ok=True,
                preview=truncate_preview(tool_output),
                data={
                    "capture_id": capture_id,
                    "tool_name": tool_name,
                    "category": category,
                    "content_bytes": len(tool_output.encode("utf-8")),
                    "spooled": spool_path is not None,
                },
                session_id=sid,
            )

        return HookResult(action="continue")


def _configure_shadow(shadow_cfg: dict[str, Any]) -> None:
    """Configure the amplifier-data shadow store from config (opt-in, best-effort).

    Expects ``{enabled, base_url, token_file}``. The token is read from the file
    written by the gateway launcher. Any failure disables shadowing silently —
    the palace remains the source of truth regardless.
    """
    global _SHADOW_STORE
    _SHADOW_STORE = None
    if not shadow_cfg.get("enabled") or not shadow_cfg.get("base_url"):
        return
    if _AmplifierDataMemoryStore is None:
        return
    try:
        token: str | None = None
        token_file = shadow_cfg.get("token_file")
        if token_file:
            p = Path(str(token_file)).expanduser()
            if p.is_file():
                token = p.read_text(encoding="utf-8").strip() or None
        _SHADOW_STORE = _AmplifierDataMemoryStore(
            base_url=str(shadow_cfg["base_url"]), token=token
        )
    except Exception:
        _SHADOW_STORE = None


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the mempalace-capture hook into the Amplifier coordinator.

    Side effect: registers the contributor, wires the coordinator bridge,
    starts the drain thread (idempotent), and replays any spool entries
    left over from a prior crashed run of the same session.
    Amplifier's native session re-hydration restores the spool dir; we
    just sweep it.
    """
    global _DRAIN_BRIDGE

    cfg = config or {}

    register_events(
        coordinator,
        "memory-mempalace-capture",
        ["memory-mempalace:drawer_filed", "memory-mempalace:capture_failed"],
    )

    bridge_emit = make_sync_bridge(coordinator)
    _DRAIN_BRIDGE = bridge_emit

    # Optional "running shadow": dual-write every filed drawer to amplifier-data
    # through the authed gateway. Opt-in, best-effort, never blocks capture.
    _configure_shadow(cfg.get("shadow_gateway") or {})

    hook = MempalaceCaptureHook(cfg, bridge_emit=bridge_emit)
    for event in hook.events:
        coordinator.hooks.register(event, hook, name=hook.name)

    _ensure_drain_thread()
    try:
        _replay_orphans(
            os.environ.get("AMPLIFIER_SESSION_ID"),
            emit_events=hook.emit_events,
        )
    except Exception:
        # Replay is best-effort; never block mount.
        pass

    return {
        "name": "hooks-mempalace-capture",
        "version": "1.2.0",
        "provides": ["mempalace-capture"],
    }
