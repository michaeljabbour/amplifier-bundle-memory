"""
amplifier-module-hooks-memory-capture

Amplifier hook that fires on tool:post events and automatically files
verbatim tool outputs as memory drawers. Wing is auto-detected from
the git remote or cwd project name; room is derived from the tool name
and active task context.

This module consolidates two previously separate hooks:
  - hooks-memory-capture (lightweight heuristic category detection)
  - hooks-memory-capture (verbatim drawer filing)

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
(``git remote get-url`` for wing detection, and the memory daemon call
for the drawer write) entirely off the hot path.

Coordinator bridging (native-cutover seam fix, 2026-07)
--------------------------------------------------------
The drain thread MUST NOT emit coordinator events directly. Doing so
(the pre-fix design) means the coroutine scheduled via
``run_coroutine_threadsafe`` runs with no session/contextvars context --
the memory-side JSONL log (``emit_event``) still worked because it takes
an explicit ``session_id`` argument, but the bridged coordinator event
(``coordinator.hooks.emit`` -> downstream observability hooks that resolve
"the current session's events.jsonl") had nothing to resolve against, and
died invisibly.  ``memory:drawer_filed`` never once appeared in ANY
session's ``events.jsonl`` as a result -- confirmed across the DTU's
entire history.

The fix: never call the coordinator bridge from the drain thread. Instead:

1. At enqueue time (``__call__``, the hot path, running on the event loop
   with the real session context intact), create a per-job
   ``asyncio.Future`` and attach it to the job (``_CaptureJob.completion_future``).
2. Also at enqueue time, schedule a task (``_await_and_bridge``) that awaits
   that future. Because the task is created via ``asyncio.ensure_future``
   from within the hot-path coroutine, it inherits the SAME context
   (``contextvars.copy_context()`` semantics of ``asyncio.Task``) --  i.e.
   the real session context, not an empty one.
3. The drain thread does its slow work as before, but instead of bridging
   directly, it resolves the future via ``loop.call_soon_threadsafe`` (see
   ``_resolve_future``) -- thread-safe, and tolerant of a future that's
   already done/cancelled (e.g. the session ended first).
4. The awaiting task (still running in the ORIGINAL hot-path context) then
   bridges the coordinator event once the future resolves.

``_replay_orphans`` (invoked from ``mount()``, also on the loop) uses the
same wiring so replayed captures get the same treatment.

Durability
----------
We rely on amplifier's native session re-hydration as the recovery
mechanism.  Each queued capture is spooled to ``~/.amplifier/memory/spool/{sid}/``
and an entry recorded in the session event log.  The drain worker deletes
the spool file on ``drawer_filed`` or ``capture_failed``.  On ``mount``
the module sweeps the spool dir for the resolved session id and re-enqueues
anything orphaned by a prior crash \u2014 when amplifier resumes the session,
the spool dir is restored alongside the event log, and we pick up where
we left off.

Credits: built on lessons from prior verbatim-memory research.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    from amplifier_module_tool_memory.event_emitter import (
        _resolve_session_id as _emitter_resolve_session_id,
    )
    from amplifier_module_tool_memory.event_emitter import (
        emit_event,
        read_events,
        truncate_preview,
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
    from amplifier_module_tool_memory.coordinator_bridge import (
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
    from amplifier_module_tool_memory.manifest import (
        load_manifest as _load_manifest,
    )
except ImportError:
    _load_manifest = None  # type: ignore[assignment]

# Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): this
# hook's write path now goes through MemoryClient via ensure_daemon() -- the
# auto-started memory daemon IS the store, not a shadow of one. This is a
# hard dependency (amplifier-module-tool-memory already hard-depends on
# amplifier-data + fastembed as of B2, \u00a78), so no defensive ImportError
# fallback here -- a failure to import means the environment is genuinely
# misconfigured, not something a private duplicate helper should paper over.
from amplifier_module_tool_memory.client import ensure_daemon


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
            # Extract repo name from URL: github.com/user/repo-name \u2192 repo-name
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


def _is_memory_worthy(tool_name: str, output: str) -> bool:
    """Heuristic: is this output worth filing as a drawer?"""
    if not output or len(output) < 50:
        return False
    # Skip noisy or binary-like outputs
    skip_tools = {"memory_status", "memory_reconnect", "memory_hook_settings"}
    if tool_name in skip_tools:
        return False
    # Skip very long outputs (>8KB) \u2014 too noisy for verbatim storage
    if len(output) > 8192:
        return False
    return True


def _skip_reason(tool_name: str, output: str) -> str:
    """Return the reason why an output is not memory-worthy."""
    if not output or len(output) < 50:
        return "too_short"
    skip_tools = {"memory_status", "memory_reconnect", "memory_hook_settings"}
    if tool_name in skip_tools:
        return "skip_tool"
    if len(output) > 8192:
        return "too_long"
    return "too_short"


def _coerce_output(raw: Any) -> str:
    """Coerce a ToolResult.output value into the str this hook stores."""
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    return json.dumps(raw)


def _extract_outcome(data: dict[str, Any]) -> tuple[str, bool]:
    """Extract ``(tool_output, tool_success)`` from a ``tool:post`` payload.

    Real contract (verified against amplifier-module-loop-streaming, both the
    sequential and the parallel tool-execution paths): the payload is
    ``{"tool_name", "tool_call_id", "tool_input", "result", "parallel_group_id"}``
    where ``result`` is ``ToolResult.model_dump()`` -- i.e.
    ``{"success": bool, "output": Any | None, "error": dict | None}``. There is
    NO top-level ``tool_output``/``is_error``/``success`` key in that shape.

    Reading those top-level keys directly (the pre-fix behavior) always
    produced an empty string and outcome-blind defaults, which gated every
    live-session capture as ``too_short`` -- ambient capture never fired in a
    real session (DTU-validated). This function reads the real nested shape
    first, with two fallbacks for robustness:

    1. ``result`` as a plain object with a ``.output`` attribute (cheap
       ``getattr`` path) -- covers a hook invoked directly against a
       ``ToolResult`` instance rather than its serialized dict.
    2. ``result`` as a plain string -- covers loop-streaming's own fallback
       (``str(result)`` when the tool result has no ``model_dump``). No
       outcome signal is available in that shape, so success defaults True.

    Only when none of these apply do we fall back to the legacy flat shape
    (``data["tool_output"]`` / ``data["is_error"]`` / ``data["success"]``)
    directly on the payload -- this is NOT the real orchestrator contract,
    but keeps existing direct callers (tests, potential future orchestrator
    variants) working rather than silently reading nothing.
    """
    result = data.get("result")

    if isinstance(result, dict):
        tool_output = _coerce_output(result.get("output"))
        tool_success = bool(result.get("success", not result.get("error")))
        return tool_output, tool_success

    if isinstance(result, str):
        # loop-streaming's `str(result)` fallback path -- no outcome signal.
        return result, True

    if result is not None and hasattr(result, "output"):
        tool_output = _coerce_output(getattr(result, "output", None))
        success_attr = getattr(result, "success", None)
        error_attr = getattr(result, "error", None)
        tool_success = bool(success_attr if success_attr is not None else not error_attr)
        return tool_output, tool_success

    # Legacy/direct-caller shape -- flat keys directly on the payload.
    tool_output = str(data.get("tool_output", ""))
    is_error = bool(data.get("is_error", False))
    tool_success = bool(data.get("success", not is_error))
    return tool_output, tool_success


def _file_drawer(wing: str, room: str, content: str, source: str, category: str | None) -> None:
    """File a verbatim drawer via the native memory daemon (client.remember).

    Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md):
    this is now the ONLY store write for the capture pipeline -- there is
    no separate shadow write anymore, since the daemon IS the store. Raises
    on any failure (daemon unavailable, or a genuine write error) so
    ``_process_job``'s existing ``except Exception`` branch continues to do
    its job: emit ``capture_failed`` and leave the spool entry in place for
    a future replay -- unchanged contract, native transport.
    """
    client = ensure_daemon()
    if client is None:
        raise RuntimeError("memory daemon unavailable")
    client.remember(wing=wing, room=room, content=content, source=source, category=category)


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


def _detect_category(text: str, signals: dict[str, list[str]] | None = None) -> str | None:
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
# A single daemon thread (_DRAIN_THREAD) does the slow work \u2014 git subprocess
# for wing detection and the memory daemon call for the drawer write.
# Module-level state is appropriate here: hooks are per-process and we want
# exactly one drain thread per process regardless of how many sessions run.
#
# Coordinator bridging happens NOWHERE in the drain thread (see the module
# docstring's "Coordinator bridging" section) -- the drain thread only ever
# resolves a job's ``completion_future`` (thread-safe, via
# ``loop.call_soon_threadsafe``). The actual bridge call happens in
# ``_await_and_bridge``, a task created on the hot-path's event loop/context.

_QUEUE_MAXSIZE = 1024  # bounded \u2014 drop with a recorded event on overflow
_QUEUE: queue.Queue[_CaptureJob] | None = None
_DRAIN_THREAD: threading.Thread | None = None
_DRAIN_LOCK = threading.Lock()
# Bridge used for replay-orphan jobs enqueued from mount() (also on the
# loop, never the drain thread). Set by mount().
_DRAIN_BRIDGE: SyncBridge = NOOP_SYNC_BRIDGE

# Strong references to in-flight "await completion future, then bridge"
# tasks. asyncio only holds a weak reference to a task once it's no longer
# referenced elsewhere, so a fire-and-forget task can be garbage collected
# mid-execution; this set keeps them alive until they finish, then the
# task's own done-callback discards them.
_PENDING_BRIDGE_TASKS: set[asyncio.Task[None]] = set()


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
    # Outcome-awareness (T1-MEM-1): whether the captured tool call succeeded.
    # Defaults to True so payloads with no outcome key behave as before.
    # tool_success=False maps to signals["unresolved"]=True downstream, which
    # *boosts* importance (a failed tool is more memory-worthy, not less).
    tool_success: bool = True
    spool_path: str | None = field(default=None, compare=False)
    # Coordinator-bridge completion signal (native-cutover seam fix). Never
    # spooled to disk (not JSON-serializable, and process-local anyway) --
    # explicitly popped before every ``_spool_write`` call. Populated by
    # ``_wire_completion_bridge`` immediately before a job is enqueued;
    # ``None`` means "no coordinator bridge wanted for this job" (either
    # emit_events=False, or a legacy/direct caller that built a job by hand).
    completion_future: asyncio.Future[dict[str, Any]] | None = field(
        default=None, compare=False, repr=False
    )


def _track_bridge_task(task: asyncio.Task[None]) -> None:
    """Keep a strong reference to a fire-and-forget bridge task until it
    completes. See ``_PENDING_BRIDGE_TASKS`` docstring for why this exists.
    """
    _PENDING_BRIDGE_TASKS.add(task)
    task.add_done_callback(_PENDING_BRIDGE_TASKS.discard)


async def _await_and_bridge(
    future: asyncio.Future[dict[str, Any]],
    capture_id: str,
    session_id: str | None,
    bridge_emit: SyncBridge,
) -> None:
    """Await the drain thread's completion signal for one capture and
    bridge the resulting coordinator event from THIS coroutine's context.

    This coroutine is scheduled (via ``asyncio.ensure_future``) from the
    hot-path ``tool:post`` handler or from ``mount()``'s orphan-replay sweep
    -- i.e. it runs with the REAL session context, unlike the foreign drain
    thread the bridge call used to be made from. That is the entire fix for
    the seam where ``memory:drawer_filed`` bridged from ``_drain_loop`` /
    ``_process_job`` never reached a session's events.jsonl.

    Tolerant of:

    * Cancellation -- the session ended (and its tasks were cancelled)
      before the drain thread finished. The memory-side JSONL log
      (``emit_event``) already recorded the real outcome; there's simply no
      coordinator event left to bridge. Not an error -- swallowed quietly.
    * Any other exception -- never propagates out of a fire-and-forget task.

    ``session_id`` is always attached to the bridged payload regardless of
    what the drain thread's envelope contains, per the fix's requirement
    that bridged payloads always carry the session id.
    """
    try:
        envelope = await future
    except asyncio.CancelledError:
        return
    except Exception:
        return

    event_name = envelope.pop("event", "memory:drawer_filed")
    envelope.setdefault("capture_id", capture_id)
    envelope["session_id"] = session_id
    try:
        bridge_emit(event_name, envelope)
    except Exception:
        pass


def _wire_completion_bridge(job: _CaptureJob, bridge_emit: SyncBridge) -> _CaptureJob:
    """Attach a completion future to *job* and schedule the awaiting bridge
    task, both on the CURRENTLY running loop and in the CURRENT context.

    Must be called from a coroutine running on the target event loop (the
    hot-path ``tool:post`` handler, or ``mount()``'s replay-orphans sweep)
    so the scheduled task inherits the SAME contextvars context as the
    caller -- the real session context, not an empty one.

    No-op (returns *job* unchanged, no future attached, no task scheduled)
    when ``job.emit_events`` is False -- matches the pre-existing contract
    that emit_events=False suppresses both the private-JSONL and
    coordinator channels.
    """
    if not job.emit_events:
        return job

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    wired = dataclasses.replace(job, completion_future=fut)

    task = asyncio.ensure_future(
        _await_and_bridge(fut, wired.capture_id, wired.session_id, bridge_emit)
    )
    _track_bridge_task(task)
    return wired


def _resolve_future(
    future: asyncio.Future[dict[str, Any]] | None, envelope: dict[str, Any]
) -> None:
    """Thread-safe resolution of a job's completion future from the drain
    thread (or the worker-exception fallback in ``_drain_loop``).

    A raw ``future.set_result()`` call from a non-owning thread is unsafe --
    asyncio futures are not thread-safe. ``call_soon_threadsafe`` is the
    correct primitive. Tolerates the future already being done/cancelled
    (e.g. the awaiting task/session went away before the drain thread
    finished) -- never raises regardless of the future's state or the
    loop's.
    """
    if future is None:
        return
    try:
        loop = future.get_loop()
    except Exception:
        return

    def _set() -> None:
        if not future.done():
            future.set_result(envelope)

    try:
        loop.call_soon_threadsafe(_set)
    except Exception:
        pass


def _spool_dir_for(session_id: str | None) -> Path | None:
    """Return ~/.amplifier/memory/spool/{sid}/ (override: AMPLIFIER_MEMORY_HOME),
    created lazily.

    Native cutover: memory owns its home outright -- there is no "initialised"
    signal to check anymore (mirrors the event emitter's home-dir semantics).
    """
    from amplifier_module_tool_memory.daemon import default_memory_home

    base = default_memory_home()
    sid = _emitter_resolve_session_id(session_id)
    spool_dir = base / "spool" / sid
    return spool_dir


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


def _ensure_drain_thread() -> queue.Queue[_CaptureJob]:
    """Lazily start the drain thread on first use. Idempotent + thread-safe."""
    global _QUEUE, _DRAIN_THREAD
    with _DRAIN_LOCK:
        if _QUEUE is None:
            _QUEUE = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        if _DRAIN_THREAD is None or not _DRAIN_THREAD.is_alive():
            _DRAIN_THREAD = threading.Thread(
                target=_drain_loop,
                name="memory-capture-drain",
                daemon=True,
            )
            _DRAIN_THREAD.start()
        return _QUEUE


def _drain_loop() -> None:
    """Background worker: pull jobs and do the slow work.

    The worker MUST NOT die.  Any exception inside one job is swallowed and
    recorded as ``capture_failed``; the loop continues forever.

    This thread NEVER calls a coordinator bridge directly (see the module
    docstring) -- it only resolves ``job.completion_future`` via
    ``_resolve_future``, which is thread-safe and tolerant of a future
    that's already done or whose loop is gone.
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
                        "memory-capture",
                        "capture_failed",
                        ok=False,
                        preview=truncate_preview(job.tool_output),
                        data={
                            "capture_id": job.capture_id,
                            "reason": "worker_exception",
                        },
                        session_id=job.session_id,
                    )
                _resolve_future(
                    job.completion_future,
                    {
                        "event": "memory:capture_failed",
                        "capture_id": job.capture_id,
                        "reason": "worker_exception",
                    },
                )
            except Exception:
                pass
            _spool_delete(job.spool_path)
        finally:
            try:
                _QUEUE.task_done()
            except Exception:
                pass


def _process_job(job: _CaptureJob) -> None:
    """Do one capture's slow work: detect wing, file drawer, emit completion."""
    wing = _detect_wing() if job.auto_wing else job.config_wing
    base_room = _detect_room(job.tool_name, job.tool_input) if job.auto_room else job.config_room
    room = f"{base_room}-{job.category}" if job.category else base_room

    try:
        _file_drawer(wing, room, job.tool_output, job.source, job.category)
        if job.emit_events:
            emit_event(
                "memory-capture",
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
                    "tool_success": job.tool_success,
                },
                session_id=job.session_id,
            )
        _resolve_future(
            job.completion_future,
            {
                "event": "memory:drawer_filed",
                "capture_id": job.capture_id,
                "wing": wing,
                "room": room,
                "category": job.category,
                "content_bytes": len(job.tool_output.encode("utf-8")),
                "source": job.source,
                "tool_success": job.tool_success,
                "ok": True,
                "preview": truncate_preview(job.tool_output),
            },
        )
        _spool_delete(job.spool_path)
    except Exception:
        if job.emit_events:
            emit_event(
                "memory-capture",
                "capture_failed",
                ok=False,
                preview=truncate_preview(job.tool_output),
                data={"capture_id": job.capture_id, "reason": "mcp_error"},
                session_id=job.session_id,
            )
        _resolve_future(
            job.completion_future,
            {
                "event": "memory:capture_failed",
                "capture_id": job.capture_id,
                "reason": "mcp_error",
            },
        )
        # Leave the spool entry in place so a future resume can retry.


def _replay_orphans(session_id: str | None, *, emit_events: bool) -> int:
    """Re-enqueue spool entries with no completion event.

    Called from ``mount``.  Uses amplifier's native session re-hydration:
    when a session resumes, the event log AND the spool dir are restored
    intact, so we can detect captures that were queued but never finished
    and put them back on the work queue.

    Runs on the event loop (invoked synchronously from within ``mount()``,
    itself a coroutine), so replayed jobs get the same completion-future
    bridging as hot-path captures via ``_wire_completion_bridge`` -- using
    ``_DRAIN_BRIDGE``, the bridge captured at mount time.
    """
    spool_dir = _spool_dir_for(session_id)
    if spool_dir is None or not spool_dir.exists():
        return 0

    completed_ids: set[str] = set()
    for ev_name in ("drawer_filed", "capture_failed"):
        for ev in read_events(
            session_id,
            hook_filter="memory-capture",
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
            # Corrupt spool file \u2014 drop it silently.
            _spool_delete(spool_file)
            continue
        job = _wire_completion_bridge(job, _DRAIN_BRIDGE)
        try:
            work_queue.put_nowait(job)
            replayed += 1
        except queue.Full:
            if job.completion_future is not None and not job.completion_future.done():
                job.completion_future.cancel()
            # We'll get another chance on the next mount.
            break

    if replayed and emit_events:
        emit_event(
            "memory-capture",
            "replay_enqueued",
            ok=True,
            data={"count": replayed},
            session_id=session_id,
        )
    return replayed


class MemoryCaptureHook:
    name = "hooks-memory-capture"
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
        # Categories to capture (empty list = capture all memory-worthy content)
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
        # Coordinator bridge \u2014 no-op default keeps the drain thread safe in tests
        self._bridge_emit: SyncBridge = bridge_emit or NOOP_SYNC_BRIDGE

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        """Hot-path handler.

        Does only cheap, deterministic work: heuristic gate, category
        detection, spool the payload, emit ``capture_queued``, enqueue,
        and wire the coordinator-bridge completion future/task (see
        ``_wire_completion_bridge``). Returns immediately.  All slow work
        \u2014 git subprocess, the memory daemon call \u2014 happens in the drain
        thread; the drain thread never bridges coordinator events itself.
        """
        tool_name: str = data.get("tool_name", "unknown")
        tool_input: dict[str, Any] = data.get("tool_input", {}) or {}
        sid = data.get("session_id")

        # T1-MEM-1 + orchestrator-contract fix: read the tool outcome BEFORE
        # the worthiness gate so the signal is known regardless of whether the
        # drawer is filed. _extract_outcome reads the REAL amplifier-core /
        # loop-streaming payload shape (data["result"] = ToolResult.model_dump())
        # with a legacy flat-shape fallback -- see its docstring for the full
        # contract and why the fallback exists.
        tool_output, tool_success = _extract_outcome(data)

        if not _is_memory_worthy(tool_name, tool_output):
            reason = _skip_reason(tool_name, tool_output)
            if self.emit_events:
                emit_event(
                    "memory-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output) if tool_output else None,
                    data={"reason": reason},
                    session_id=sid,
                )
                try:
                    self._bridge_emit(
                        "memory:capture_skipped",
                        {
                            "reason": reason,
                            "tool_name": tool_name,
                            "ok": False,
                        },
                    )
                except Exception:
                    pass
            return HookResult(action="continue")

        category = _detect_category(tool_output, self._signals)
        if self.categories and category not in self.categories:
            if self.emit_events:
                emit_event(
                    "memory-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output),
                    data={"reason": "category_filtered"},
                    session_id=sid,
                )
                try:
                    self._bridge_emit(
                        "memory:capture_skipped",
                        {
                            "reason": "category_filtered",
                            "tool_name": tool_name,
                            "category": category,
                            "ok": False,
                        },
                    )
                except Exception:
                    pass
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
            tool_success=tool_success,
        )

        # Spool to disk so a crash leaves the work recoverable on next mount.
        # completion_future is never spooled -- it's process-local and not
        # JSON-serializable, and at this point it's still None regardless
        # (wired in below, after spooling).
        spool_dir = _spool_dir_for(sid)
        spool_path: Path | None = None
        if spool_dir is not None:
            try:
                payload = asdict(job)
                payload.pop("spool_path", None)
                payload.pop("completion_future", None)
                spool_path = _spool_write(spool_dir, capture_id, payload)
            except Exception:
                spool_path = None  # spool failure is non-fatal \u2014 we still queue
        if spool_path is not None:
            job = dataclasses.replace(job, spool_path=str(spool_path))

        # Wire the coordinator-bridge completion future BEFORE enqueueing --
        # the drain thread must see it on the exact job object it pops.
        # This is the fix: the future/task are created HERE, on the hot
        # path's event loop and in the hot path's context, not inside the
        # foreign drain thread. See module docstring + _wire_completion_bridge.
        job = _wire_completion_bridge(job, self._bridge_emit)

        work_queue = _ensure_drain_thread()
        try:
            work_queue.put_nowait(job)
        except queue.Full:
            # Backpressure: the worker is behind. Don't block the kernel.
            # Drop with a visible event; the spool entry remains so a future
            # mount-time replay can pick it up. Cancel the just-created
            # future/task so it doesn't wait forever for a job that will
            # never be processed.
            if job.completion_future is not None and not job.completion_future.done():
                job.completion_future.cancel()
            if self.emit_events:
                emit_event(
                    "memory-capture",
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
                "memory-capture",
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


async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mount the memory-capture hook into the Amplifier coordinator.

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
        "memory-capture",
        ["memory:drawer_filed", "memory:capture_failed", "memory:capture_skipped"],
    )

    bridge_emit = make_sync_bridge(coordinator)
    _DRAIN_BRIDGE = bridge_emit

    hook = MemoryCaptureHook(cfg, bridge_emit=bridge_emit)
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
        "name": "hooks-memory-capture",
        "version": "1.2.3",
        "provides": ["memory-capture"],
    }
