"""
amplifier-module-hooks-memory-reflect

Cold-path memory reflection (T2.5, docs/plans/2026-09-27-memory-layers-design.md
§2 "cold path", §3 shape table, §4 T2.5). Watermarks the conversation span
since the last reflection and queues it as a durable ``reflection_job`` cell;
optionally spawns the ``memory:distiller`` agent to process it immediately.

Events observed (D7, PROVENANCE.md): ``context:compaction`` (emitted by
foundation ``context-simple`` with a stats-only payload -- no evicted-message
payload is part of any contract, so this hook re-reads the full history
itself), ``context:pre_compact`` (kernel constant; emitted by other context
managers), ``session:end`` and ``session:start`` (drain).

Hot-path discipline: every handler does only cheap, synchronous work (child
session check, watermark slice, size gate) and returns
``HookResult(action="continue")`` immediately. The actual store write and
any agent spawn run inside an ``asyncio.Task`` tracked in
``_pending_tasks`` so a session ending mid-task doesn't orphan it silently --
``mount()`` registers a ``coordinator.register_cleanup`` callback that awaits
those tasks (bounded by ``drain_timeout_s``) before cancelling whatever is
left. A cancelled/never-run task simply leaves its job ``pending`` in the
durable store -- nothing is lost, it is picked up by the next session's
``drain_on_start`` sweep (or an on-demand "reflect on pending memory jobs").

Child-session guard: this hook spawns ``memory:distiller`` as a child
session. Without a guard, the distiller's own session would ALSO observe
``context:compaction``/``session:end`` and try to reflect-on-a-reflection --
unbounded recursion. The kernel gives every spawned session a ``parent_id``
(``amplifier_core.session.AmplifierSession(parent_id=...)``, set by
``amplifier_app_cli.session_spawner.spawn_sub_session``); the coordinator
exposes this as the read-only ``coordinator.parent_id`` property
(``amplifier_core/_engine.pyi:177``, mirrored on ``RustSession`` at line 45).
``coordinator.parent_id is not None`` is therefore an exact, first-class
"am I a child session" check -- this hook no-ops immediately whenever it is
true, before doing anything else.

Store capability probe: ``reflection_job_add``/``reflection_jobs`` are new
tool-memory ops (also T2.5, landing concurrently with this module in a
sibling PR). Every store call is guarded with ``hasattr`` so this hook
degrades to a clean ``store_unsupported`` skip against an older pinned
daemon, exactly like ``hooks-memory-capture``'s ``TypeError``-retry pattern
for the same kind of version skew.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar

try:
    from amplifier_core import HookResult  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests).
    class HookResult:  # type: ignore
        def __init__(self, *, action: str = "continue", **kwargs: Any) -> None:
            self.action = action
            for k, v in kwargs.items():
                setattr(self, k, v)


try:
    from amplifier_module_tool_memory.coordinator_bridge import (
        register_events,
    )
except ImportError:

    def register_events(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


try:
    from amplifier_module_tool_memory.event_emitter import emit_event
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


# Hard dependency, mirrors hooks-memory-capture's native cutover: tool-memory
# already hard-depends on amplifier-data + fastembed, so no defensive
# ImportError fallback for the daemon client itself.
from amplifier_module_tool_memory.client import ensure_daemon

try:
    # Prefer the capture hook's wing detector -- one implementation of "what
    # project is this" for the whole bundle. hooks-memory-capture is NOT the
    # module another builder is concurrently editing (only tool-memory is),
    # so this import is safe. Falls back to a minimal duplicate (below) if
    # the capture hook isn't installed/importable for some reason.
    from amplifier_module_hooks_memory_capture import _detect_wing
except ImportError:
    import os
    import subprocess
    from pathlib import Path

    def _detect_wing(cwd: str | None = None) -> str:  # type: ignore[misc]
        """Minimal duplicate of hooks-memory-capture._detect_wing.

        Kept intentionally tiny -- this only fires when the capture hook
        isn't installed at all, which is not a supported deployment (memory
        reflection composes on top of behaviors/memory.yaml, which always
        includes hooks-memory-capture).
        """
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd or os.getcwd(),
                check=False,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                name = url.rstrip("/").split("/")[-1].replace(".git", "")
                return f"wing_{name}" if name else "wing_general"
        except Exception:  # noqa: BLE001 -- best-effort fallback, never raises
            pass
        cwd_path = Path(cwd or os.getcwd())
        return f"wing_{cwd_path.name}"


# ---------------------------------------------------------------------------
# Memory-injection markers (skip these from the reflected span -- they are
# ephemeral orientation text, not conversation content, and reflecting on
# them would let the briefing/interject hooks' own output leak back in as
# "facts"). Exact prefixes verified against the emitting hooks:
#   - hooks-memory-briefing: header = f"## Memory Briefing -- `{project}`\n"
#   - hooks-memory-interject: header = "\U0001f4da Relevant memory from a
#     previous session:"
# ---------------------------------------------------------------------------
_BRIEFING_MARKER = "## Memory Briefing"
_INTERJECT_MARKER = "\U0001f4da Relevant memory from a previous session:"


def _is_memory_injection(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith((_BRIEFING_MARKER, _INTERJECT_MARKER))


def _flatten_content(content: Any) -> str:
    """Render a message's ``content`` field to plain text.

    ``content`` is a plain string for amplifier-core's normalized message
    shape (``message_models.py``: roles system/developer/user/assistant/
    function/tool). Defensively also handles a list-of-blocks shape (some
    providers/tests pass Anthropic-style content blocks) by joining any
    ``text`` fields and stringifying anything else recognizable.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), (str, list)):
                    parts.append(_flatten_content(item["content"]))
        return "\n".join(p for p in parts if p)
    return str(content)


def _render_span(
    messages: list[dict[str, Any]],
    *,
    max_tool_result_chars: int,
    max_span_chars: int,
) -> str:
    """Render a message span to the plain text a distiller agent reads.

    One line per message: ``"[role] content"``. System messages and
    anything that looks like a memory injection are skipped entirely. Tool
    results (``role == "tool"``) are truncated to ``max_tool_result_chars``.
    The whole span is capped at ``max_span_chars``, keeping the NEWEST
    content (a truncation marker replaces the dropped prefix) -- the most
    recent messages are the ones most likely to hold undistilled facts.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        text = _flatten_content(msg.get("content"))
        if not text:
            continue
        if _is_memory_injection(text):
            continue
        if role == "tool" and len(text) > max_tool_result_chars:
            text = text[:max_tool_result_chars] + "...[truncated]"
        lines.append(f"[{role}] {text}")

    rendered = "\n".join(lines)
    if len(rendered) > max_span_chars:
        rendered = "...[earlier content truncated]...\n" + rendered[-max_span_chars:]
    return rendered


def _count_non_system(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m.get("role") != "system")


class MemoryReflectHook:
    name = "hooks-memory-reflect"
    events: ClassVar[list[str]] = [
        "context:compaction",
        "context:pre_compact",
        "session:end",
        "session:start",
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.enabled: bool = bool(self.config.get("enabled", True))
        self.min_new_messages: int = int(self.config.get("min_new_messages", 6))
        self.max_span_chars: int = int(self.config.get("max_span_chars", 24000))
        self.max_tool_result_chars: int = int(
            self.config.get("max_tool_result_chars", 1500)
        )
        self.max_spawns_per_session: int = int(
            self.config.get("max_spawns_per_session", 4)
        )
        self.agent: str = self.config.get("agent", "memory:distiller")
        self.drain_on_start: bool = bool(self.config.get("drain_on_start", True))
        self.drain_limit: int = int(self.config.get("drain_limit", 3))
        self.drain_timeout_s: float = float(self.config.get("drain_timeout_s", 20))
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        # P3 (T3.2/D19): opt-in curator spawn to refresh the L3 index/standing
        # answers. Default OFF -- the derived index (D19: mechanism-first) is
        # always available without this; this only makes the CURATED version
        # better. Never blocks session:start (same background-task pattern as
        # drain_on_start).
        self.index_refresh: bool = bool(self.config.get("index_refresh", False))
        self.index_refresh_min_pending: int = int(
            self.config.get("index_refresh_min_pending", 10)
        )

        self._watermark = 0
        self._spawns_this_session = 0
        self._lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._coordinator: Any = None

    # -- helpers -----------------------------------------------------------

    def _track(self, task: asyncio.Task[None]) -> None:
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _is_child_session(self, coordinator: Any) -> bool:
        """True iff this session was spawned by another (parent_id set).

        See module docstring "Child-session guard" for the verified
        provenance of ``coordinator.parent_id``
        (amplifier_core/_engine.pyi:177, RustSession property at :45,
        set via amplifier_app_cli/session_spawner.py:538
        ``AmplifierSession(parent_id=parent_session.session_id, ...)``).
        """
        return getattr(coordinator, "parent_id", None) is not None

    def _emit(
        self, session_id: str | None, event: str, *, ok: bool, data: dict[str, Any]
    ) -> None:
        if not self.emit_events:
            return
        emit_event(self.name, event, ok=ok, data=data, session_id=session_id)

    async def _get_store_client(self) -> Any | None:
        try:
            return ensure_daemon()
        except Exception:
            return None

    async def _queue_reflection(
        self,
        coordinator: Any,
        *,
        trigger: str,
        session_id: str | None,
        allow_spawn: bool,
    ) -> None:
        """Do the actual (off-hot-path) work: render span, queue job, maybe spawn.

        Runs inside a tracked ``asyncio.Task`` -- never awaited directly by
        an event handler.
        """
        async with self._lock:
            context = coordinator.get("context") if coordinator is not None else None
            if context is None:
                return
            try:
                messages: list[dict[str, Any]] = await context.get_messages()
            except Exception:
                return

            span = messages[self._watermark :]
            if _count_non_system(span) < self.min_new_messages:
                self._emit(
                    session_id,
                    "memory:reflection_skipped",
                    ok=False,
                    data={"reason": "too_small", "trigger": trigger},
                )
                return

            span_text = _render_span(
                span,
                max_tool_result_chars=self.max_tool_result_chars,
                max_span_chars=self.max_span_chars,
            )
            if not span_text:
                self._emit(
                    session_id,
                    "memory:reflection_skipped",
                    ok=False,
                    data={"reason": "too_small", "trigger": trigger},
                )
                return

            client = await self._get_store_client()
            if client is None or not hasattr(client, "reflection_job_add"):
                self._emit(
                    session_id,
                    "memory:reflection_skipped",
                    ok=False,
                    data={"reason": "store_unsupported", "trigger": trigger},
                )
                return

            wing = _detect_wing()
            observed_at = datetime.now(UTC).isoformat()

            try:
                result = client.reflection_job_add(
                    span_text=span_text,
                    session_id=session_id,
                    trigger=trigger,
                    wing=wing,
                    room=None,
                    observed_at=observed_at,
                )
            except Exception:
                self._emit(
                    session_id,
                    "memory:reflection_failed",
                    ok=False,
                    data={"reason": "store_error", "trigger": trigger},
                )
                return

            job_ref = (result or {}).get("job_ref")
            # Only advance the watermark once the job is durably queued --
            # a store failure above must be retried from the same point,
            # never silently skip the span it failed to record.
            self._watermark = len(messages)

            self._emit(
                session_id,
                "memory:reflection_queued",
                ok=True,
                data={
                    "job_ref": job_ref,
                    "trigger": trigger,
                    "messages": len(span),
                },
            )

            if trigger == "session:end" or not allow_spawn or job_ref is None:
                return

            await self._maybe_spawn(coordinator, job_ref=job_ref, session_id=session_id)

    async def _maybe_spawn(
        self, coordinator: Any, *, job_ref: str, session_id: str | None
    ) -> None:
        spawn_fn = None
        try:
            spawn_fn = coordinator.get_capability("session.spawn")
        except Exception:
            spawn_fn = None

        if spawn_fn is None:
            self._emit(
                session_id,
                "memory:reflection_skipped",
                ok=False,
                data={"reason": "no_spawn_capability", "job_ref": job_ref},
            )
            return

        if self._spawns_this_session >= self.max_spawns_per_session:
            self._emit(
                session_id,
                "memory:reflection_skipped",
                ok=False,
                data={"reason": "spawn_cap_reached", "job_ref": job_ref},
            )
            return

        self._spawns_this_session += 1
        instruction = (
            f"Process reflection job {job_ref}. Follow your procedure; "
            "finish with reflection_job_done."
        )

        self._emit(
            session_id,
            "memory:reflection_started",
            ok=True,
            data={"job_ref": job_ref, "agent": self.agent},
        )
        try:
            parent_session = getattr(coordinator, "session", None)
            agent_configs = (getattr(coordinator, "config", None) or {}).get(
                "agents", {}
            )
            await spawn_fn(
                agent_name=self.agent,
                instruction=instruction,
                parent_session=parent_session,
                agent_configs=agent_configs,
            )
        except Exception as exc:
            self._emit(
                session_id,
                "memory:reflection_failed",
                ok=False,
                data={"job_ref": job_ref, "reason": str(exc)},
            )
            return

        self._emit(
            session_id,
            "memory:reflection_completed",
            ok=True,
            data={"job_ref": job_ref, "agent": self.agent},
        )

    async def _drain_pending(self, coordinator: Any, *, session_id: str | None) -> None:
        client = await self._get_store_client()
        if client is None or not hasattr(client, "reflection_jobs"):
            self._emit(
                session_id,
                "memory:reflection_skipped",
                ok=False,
                data={"reason": "store_unsupported", "trigger": "session:start"},
            )
            return

        try:
            jobs = client.reflection_jobs(
                state="pending", wing=None, limit=self.drain_limit
            )
        except Exception:
            self._emit(
                session_id,
                "memory:reflection_failed",
                ok=False,
                data={"reason": "store_error", "trigger": "session:start"},
            )
            return

        for job in jobs or []:
            job_ref = job.get("job_ref") if isinstance(job, dict) else None
            if not job_ref:
                continue
            await self._maybe_spawn(coordinator, job_ref=job_ref, session_id=session_id)

    async def _maybe_refresh_index(
        self, coordinator: Any, *, session_id: str | None
    ) -> None:
        """P3 (T3.2/D19), opt-in: spawn ``memory:curator`` to refresh the L3
        index/standing answers when any room has drifted, or any standing
        question is stale. Never blocks -- runs in a tracked background
        task like :meth:`_drain_pending`. Capability-checked (``hasattr``),
        never a hard dependency on an index-capable daemon.
        """
        if not self.index_refresh:
            return

        client = await self._get_store_client()
        if (
            client is None
            or not hasattr(client, "index")
            or not hasattr(client, "standing")
        ):
            self._emit(
                session_id,
                "memory:index_refresh_skipped",
                ok=False,
                data={"reason": "store_unsupported"},
            )
            return

        wing = _detect_wing()

        try:
            index_rows = client.index(wing=wing) or []
        except Exception:
            index_rows = []
        stale_rooms = [
            row["scope"][len("room:") :]
            for row in index_rows
            if isinstance(row, dict)
            and row.get("source") == "derived"
            and (row.get("pending_changes", 0) or 0) >= self.index_refresh_min_pending
            and str(row.get("scope", "")).startswith("room:")
        ]

        try:
            standing_rows = client.standing(wing=wing) or []
        except Exception:
            standing_rows = []
        stale_questions = [
            row["question_ref"]
            for row in standing_rows
            if isinstance(row, dict) and row.get("stale")
        ]

        if not stale_rooms and not stale_questions:
            self._emit(
                session_id,
                "memory:index_refresh_skipped",
                ok=True,
                data={"reason": "nothing_stale", "wing": wing},
            )
            return

        spawn_fn = None
        try:
            spawn_fn = coordinator.get_capability("session.spawn")
        except Exception:
            spawn_fn = None
        if spawn_fn is None:
            self._emit(
                session_id,
                "memory:index_refresh_skipped",
                ok=False,
                data={"reason": "no_spawn_capability", "wing": wing},
            )
            return

        instruction = (
            f"Refresh the memory index for wing {wing}: rooms {stale_rooms}; "
            f"re-answer stale standing questions {stale_questions}. Use "
            "index_set/standing_answer with cites. Navigation text only."
        )
        self._emit(
            session_id,
            "memory:index_refresh_queued",
            ok=True,
            data={"wing": wing, "rooms": stale_rooms, "questions": stale_questions},
        )
        try:
            parent_session = getattr(coordinator, "session", None)
            agent_configs = (getattr(coordinator, "config", None) or {}).get(
                "agents", {}
            )
            await spawn_fn(
                agent_name="memory:curator",
                instruction=instruction,
                parent_session=parent_session,
                agent_configs=agent_configs,
            )
        except Exception as exc:
            self._emit(
                session_id,
                "memory:index_refresh_skipped",
                ok=False,
                data={"reason": "spawn_failed", "error": str(exc), "wing": wing},
            )

    # -- event handlers ------------------------------------------------------

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        if not self.enabled:
            return HookResult(action="continue")

        coordinator = self._coordinator
        if coordinator is None or self._is_child_session(coordinator):
            return HookResult(action="continue")

        session_id = data.get("session_id") if isinstance(data, dict) else None

        if event in ("context:compaction", "context:pre_compact"):
            task = asyncio.ensure_future(
                self._queue_reflection(
                    coordinator,
                    trigger=event,
                    session_id=session_id,
                    allow_spawn=True,
                )
            )
            self._track(task)
        elif event == "session:end":
            task = asyncio.ensure_future(
                self._queue_reflection(
                    coordinator,
                    trigger="session:end",
                    session_id=session_id,
                    allow_spawn=False,
                )
            )
            self._track(task)
        elif event == "session:start":
            if self.drain_on_start:
                task = asyncio.ensure_future(
                    self._drain_pending(coordinator, session_id=session_id)
                )
                self._track(task)
            if self.index_refresh:
                task = asyncio.ensure_future(
                    self._maybe_refresh_index(coordinator, session_id=session_id)
                )
                self._track(task)

        return HookResult(action="continue")


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the memory-reflect hook into the Amplifier coordinator.

    Registers the event names this hook emits, registers the four handlers,
    and registers a cleanup that awaits any in-flight reflect/spawn tasks
    (bounded by ``drain_timeout_s``) before cancelling stragglers -- a
    cancelled task simply leaves its job ``pending`` for the next session's
    ``drain_on_start`` sweep.
    """
    cfg = config or {}

    register_events(
        coordinator,
        "hooks-memory-reflect",
        [
            "memory:reflection_queued",
            "memory:reflection_started",
            "memory:reflection_completed",
            "memory:reflection_skipped",
            "memory:reflection_failed",
            "memory:index_refresh_queued",
            "memory:index_refresh_skipped",
        ],
    )

    hook = MemoryReflectHook(cfg)
    hook._coordinator = coordinator
    for event in hook.events:
        coordinator.hooks.register(event, hook, name=hook.name)

    async def cleanup() -> None:
        pending = list(hook._pending_tasks)
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=hook.drain_timeout_s)
        for task in still_pending:
            task.cancel()

    coordinator.register_cleanup(cleanup)

    return {
        "name": "hooks-memory-reflect",
        "version": "0.1.0",
        "provides": ["memory-reflect"],
    }
