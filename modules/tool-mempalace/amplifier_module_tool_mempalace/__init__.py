"""
amplifier-module-tool-mempalace
High-level Amplifier tool wrapping the MemPalace MCP server.

Provides a single `palace` tool with sub-operations:
  palace_search    — Semantic search (scoped by wing/room)
  palace_remember  — File verbatim content as a drawer
  palace_status    — Palace overview
  palace_kg        — Knowledge graph query/add/invalidate
  palace_traverse  — Graph traversal across wings
  palace_diary     — Agent diary read/write
  palace_mine      — Mine a directory or conversation file
  palace_events    — Query the per-session JSONL event log (CP2)

All 29 raw MCP tools are also available via the mempalace_* prefix
through the MCP integration in the bundle behavior.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from amplifier_core import Tool, ToolResult  # type: ignore

from .coordinator_bridge import (
    NOOP_SYNC_BRIDGE,
    SyncBridge,
    make_sync_bridge,
    register_events,
)
from .event_emitter import _read_events_with_skip_count, emit_event
from .garden import execute_garden
from .scripts.memory_store import AmplifierDataMemoryStore as _AmplifierDataMemoryStore
from .scripts.memory_store import _call_mcp_tool as _call_mcp_tool_impl

# Hard wall-clock budget for garden operations. Patchable in tests.
_GARDEN_TIMEOUT_S: float = 120.0


PALACE_PATH = Path.home() / ".mempalace"

# Best-effort amplifier-data shadow store (the "running shadow" — §4b/§4c of
# docs/plans/2026-07-07-substrate-adapter-completion-design.md): every
# successful palace ``kg add``/``kg invalidate``/``diary write`` op is
# best-effort mirrored to amplifier-data via the authed gateway. None disables
# shadowing. Set by mount() from the ``shadow_gateway`` config block — the
# SAME knob vocabulary as hooks-mempalace-capture's shadow (one config
# schema across the bundle). The client side is pure urllib
# (GatewayClient) — this process does NOT need amplifier-data installed;
# only the gateway service process does.
_SHADOW_STORE: Any = None


def _configure_shadow(shadow_cfg: dict[str, Any]) -> None:
    """Configure the amplifier-data shadow store from config (opt-in, best-effort).

    Expects ``{enabled, base_url, token_file}`` — mirrors
    hooks-mempalace-capture's ``_configure_shadow`` contract exactly. Any
    failure disables shadowing silently; the palace remains the source of
    truth regardless.
    """
    global _SHADOW_STORE
    _SHADOW_STORE = None
    if not shadow_cfg.get("enabled") or not shadow_cfg.get("base_url"):
        return
    try:
        token: str | None = None
        token_file = shadow_cfg.get("token_file")
        if token_file:
            token_path = Path(str(token_file)).expanduser()
            if token_path.is_file():
                token = token_path.read_text(encoding="utf-8").strip() or None
        _SHADOW_STORE = _AmplifierDataMemoryStore(
            base_url=str(shadow_cfg["base_url"]), token=token
        )
    except Exception:
        _SHADOW_STORE = None


def _shadow_kg(kg_action: str, subject: str, predicate: str, object: str) -> None:  # noqa: A002
    """Best-effort shadow a palace KG add/invalidate to amplifier-data.

    Never raises — the palace op has already succeeded by the time this is
    called; a shadow failure must never surface to the caller.
    """
    store = _SHADOW_STORE
    if store is None:
        return
    try:
        if kg_action == "add":
            store.assert_kg(subject, predicate, object)
        elif kg_action == "invalidate":
            store.invalidate_kg(subject, predicate, object)
        else:
            return
        emit_event(
            "tool-mempalace",
            "kg_shadow_filed",
            ok=True,
            data={"kg_action": kg_action, "subject": subject, "predicate": predicate},
        )
    except Exception as exc:  # shadow must never break the source-of-truth write
        emit_event(
            "tool-mempalace",
            "kg_shadow_failed",
            ok=False,
            data={"kg_action": kg_action, "reason": type(exc).__name__},
        )


def _shadow_diary(agent_name: str, entry: str, topic: str) -> None:
    """Best-effort shadow a palace diary write to amplifier-data. Never raises."""
    store = _SHADOW_STORE
    if store is None:
        return
    try:
        store.file_diary(agent_name=agent_name, entry=entry, topic=topic)
        emit_event(
            "tool-mempalace",
            "diary_shadow_filed",
            ok=True,
            data={"agent_name": agent_name, "topic": topic},
        )
    except Exception as exc:  # shadow must never break the source-of-truth write
        emit_event(
            "tool-mempalace",
            "diary_shadow_failed",
            ok=False,
            data={"agent_name": agent_name, "reason": type(exc).__name__},
        )


def _mcp_result_to_tool_result(result: dict[str, Any]) -> ToolResult:
    """Convert an MCP call's result dict into a contract-correct ToolResult.

    ``amplifier_core.ToolResult`` is a pydantic model with ``success`` /
    ``output`` / ``error`` fields (core:docs/contracts/TOOL_CONTRACT.md), NOT
    ``content`` / ``is_error``. Passing unknown kwargs is silently accepted
    by pydantic's permissive ``BaseModel.__init__(**data)`` and dropped --
    the caller ends up with the all-defaults ``ToolResult()`` (success=True,
    output=None) even for a hard failure. This helper is the single place
    that maps an MCP result (``{"error": ...}`` on failure, per
    ``_mcp_call``'s docstring) onto the real contract so every operation
    branch below reports failures loudly instead of silently.
    """
    error = result.get("error") if isinstance(result, dict) else None
    serialized = json.dumps(result, indent=2)
    if error:
        return ToolResult(
            success=False, output=serialized, error={"message": str(error)}
        )
    return ToolResult(success=True, output=serialized)


def _mcp_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a MemPalace MCP tool and return the result.

    Delegates to the canonical ``_call_mcp_tool`` in scripts/memory_store.py,
    which speaks the real ``mempalace-mcp`` JSON-RPC-over-stdio surface (see
    that function's docstring for why the previous ``mempalace mcp --call``
    invocation never worked against any published mempalace). Kept as a
    same-name, same-signature wrapper so every operation branch below (and
    ``execute``'s error handling) is unchanged; only the transport moved.

    Errors surface as ``{"error": "..."}``, same shape as before -- callers
    already check ``result.get("error")`` and/or serialize the dict straight
    into the ToolResult content, so a failed call is still visible to the
    caller (never silently dropped), matching this tool's existing posture.
    """
    return _call_mcp_tool_impl(tool_name, args, timeout=30.0)


class PalaceTool(Tool):
    name = "palace"
    description = (
        "MemPalace memory operations. Operations: search, remember, status, "
        "kg (knowledge graph), traverse, diary, mine, events, garden."
    )

    def __init__(self, *, bridge_emit: SyncBridge | None = None) -> None:
        super().__init__()
        self._bridge_emit: SyncBridge = bridge_emit or NOOP_SYNC_BRIDGE

    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "search",
                    "remember",
                    "status",
                    "kg",
                    "traverse",
                    "diary",
                    "mine",
                    "events",
                    "garden",
                ],
                "description": "The palace operation to perform.",
            },
            "query": {
                "type": "string",
                "description": "Search query (for 'search' operation).",
            },
            "wing": {
                "type": "string",
                "description": "Wing (project/person) to scope the operation to.",
            },
            "room": {
                "type": "string",
                "description": "Room (topic) within the wing.",
            },
            "content": {
                "type": "string",
                "description": "Verbatim content to store (for 'remember' operation).",
            },
            "limit": {
                "type": "integer",
                "description": "Result limit. Defaults: search=5, events=50 (max 200).",
                "default": 5,
            },
            # Knowledge graph parameters
            "entity": {
                "type": "string",
                "description": "Entity name for 'kg' operation.",
            },
            "subject": {
                "type": "string",
                "description": "KG fact subject (for kg add/invalidate).",
            },
            "predicate": {
                "type": "string",
                "description": "KG fact predicate (for kg add/invalidate).",
            },
            "object": {
                "type": "string",
                "description": "KG fact object (for kg add/invalidate).",
            },
            "kg_action": {
                "type": "string",
                "enum": ["query", "add", "invalidate", "timeline", "stats"],
                "description": "Knowledge graph sub-action.",
                "default": "query",
            },
            # Diary parameters
            "agent_name": {
                "type": "string",
                "description": "Agent name for diary operations.",
            },
            "entry": {
                "type": "string",
                "description": "Diary entry text (for diary write).",
            },
            "diary_action": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "Diary sub-action.",
                "default": "read",
            },
            # Traverse parameters
            "start_room": {
                "type": "string",
                "description": "Starting room for graph traversal.",
            },
            "max_hops": {
                "type": "integer",
                "description": "Max hops for traversal (default: 2).",
                "default": 2,
            },
            # Mine parameters
            "path": {
                "type": "string",
                "description": "File or directory path to mine (for 'mine' operation).",
            },
            "mode": {
                "type": "string",
                "enum": ["files", "convos"],
                "description": "Mining mode: 'files' for project files, 'convos' for conversation exports.",
                "default": "files",
            },
            # Events parameters
            "session_id": {
                "type": "string",
                "description": (
                    "Which session's events to read (events operation only). "
                    "Defaults to the current session."
                ),
            },
            "hook_filter": {
                "type": "string",
                "description": (
                    "Filter events to a specific hook "
                    "(e.g. 'mempalace-capture'). For 'events' operation."
                ),
            },
            "event_filter": {
                "type": "string",
                "description": (
                    "Filter events to a specific event type "
                    "(e.g. 'drawer_filed'). For 'events' operation."
                ),
            },
            "tail": {
                "type": "boolean",
                "description": (
                    "If true (default), return the most recent N events. "
                    "If false, return the oldest N events. For 'events' operation."
                ),
                "default": True,
            },
            # Garden parameters
            "lookback_days": {
                "type": "integer",
                "description": (
                    "Only analyze drawers added in the last N days (garden operation). "
                    "Default: 90."
                ),
                "default": 90,
            },
            "max_drawers": {
                "type": "integer",
                "description": (
                    "Budget cap: max drawers to analyze per garden run. "
                    "Default 200, hard cap 500."
                ),
                "default": 200,
            },
            "cluster_threshold": {
                "type": "number",
                "description": (
                    "Cosine similarity threshold for clustering (garden operation). "
                    "Default: 0.80."
                ),
                "default": 0.80,
            },
        },
        "required": ["operation"],
    }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        operation = input.get("operation", "")
        kwargs = {k: v for k, v in input.items() if k != "operation"}
        try:
            if operation == "search":
                args: dict[str, Any] = {
                    "query": kwargs.get("query", ""),
                    "limit": kwargs.get("limit", 5),
                }
                if kwargs.get("wing"):
                    args["wing"] = kwargs["wing"]
                if kwargs.get("room"):
                    args["room"] = kwargs["room"]
                result = _mcp_call("mempalace_search", args)
                return _mcp_result_to_tool_result(result)

            elif operation == "remember":
                args = {
                    "wing": kwargs.get("wing", "general"),
                    "room": kwargs.get("room", "notes"),
                    "content": kwargs.get("content", ""),
                    "added_by": "amplifier",
                }
                result = _mcp_call("mempalace_add_drawer", args)
                return _mcp_result_to_tool_result(result)

            elif operation == "status":
                result = _mcp_call("mempalace_status", {})
                return _mcp_result_to_tool_result(result)

            elif operation == "kg":
                kg_action = kwargs.get("kg_action", "query")
                if kg_action == "query":
                    args = {"entity": kwargs.get("entity", "")}
                    if kwargs.get("wing"):
                        args["as_of"] = kwargs.get("as_of", "")
                    result = _mcp_call("mempalace_kg_query", args)
                elif kg_action == "add":
                    args = {
                        "subject": kwargs.get("subject", ""),
                        "predicate": kwargs.get("predicate", ""),
                        "object": kwargs.get("object", ""),
                    }
                    result = _mcp_call("mempalace_kg_add", args)
                    if not result.get("error"):
                        _shadow_kg("add", args["subject"], args["predicate"], args["object"])
                elif kg_action == "invalidate":
                    args = {
                        "subject": kwargs.get("subject", ""),
                        "predicate": kwargs.get("predicate", ""),
                        "object": kwargs.get("object", ""),
                    }
                    result = _mcp_call("mempalace_kg_invalidate", args)
                    if not result.get("error"):
                        _shadow_kg(
                            "invalidate", args["subject"], args["predicate"], args["object"]
                        )
                elif kg_action == "timeline":
                    args = {"entity": kwargs.get("entity", "")}
                    result = _mcp_call("mempalace_kg_timeline", args)
                else:  # stats
                    result = _mcp_call("mempalace_kg_stats", {})
                return _mcp_result_to_tool_result(result)

            elif operation == "traverse":
                args = {
                    "start_room": kwargs.get("start_room", ""),
                    "max_hops": kwargs.get("max_hops", 2),
                }
                result = _mcp_call("mempalace_traverse", args)
                return _mcp_result_to_tool_result(result)

            elif operation == "diary":
                diary_action = kwargs.get("diary_action", "read")
                agent_name = kwargs.get("agent_name", "amplifier")
                if diary_action == "write":
                    args = {
                        "agent_name": agent_name,
                        "entry": kwargs.get("entry", ""),
                        "topic": kwargs.get("room", "general"),
                    }
                    result = _mcp_call("mempalace_diary_write", args)
                    if not result.get("error"):
                        _shadow_diary(agent_name, args["entry"], args["topic"])
                else:
                    args = {"agent_name": agent_name, "last_n": kwargs.get("limit", 10)}
                    result = _mcp_call("mempalace_diary_read", args)
                return _mcp_result_to_tool_result(result)

            elif operation == "mine":
                path = kwargs.get("path", ".")
                mode = kwargs.get("mode", "files")
                proc = subprocess.run(
                    ["mempalace", "mine", path, "--mode", mode],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                output = (proc.stdout + proc.stderr).strip()
                if proc.returncode != 0:
                    return ToolResult(
                        success=False,
                        output=output,
                        error={
                            "message": (
                                output
                                or f"mempalace mine exited with code {proc.returncode}"
                            )
                        },
                    )
                return ToolResult(success=True, output=output)

            elif operation == "events":
                # Read the per-session JSONL event log written by CP1's emitter.
                # Clamp limit to [1, 200] silently; default 50.
                raw_limit: int = int(kwargs.get("limit", 50))
                limit = max(1, min(raw_limit, 200))
                tail: bool = bool(kwargs.get("tail", True))
                sid: str | None = kwargs.get("session_id")
                hook_filter: str | None = kwargs.get("hook_filter")
                event_filter: str | None = kwargs.get("event_filter")

                # Read all matching events (up to a generous ceiling) so we
                # can report event_count (total) separately from returned (capped).
                # The helper also returns the number of corrupt lines skipped.
                all_events, skipped_lines = _read_events_with_skip_count(
                    session_id=sid,
                    hook_filter=hook_filter,
                    event_filter=event_filter,
                    limit=10_000,
                    tail=False,  # get everything in chronological order first
                )
                event_count = len(all_events)

                # Apply tail / head and final limit
                if tail:
                    page = all_events[-limit:] if event_count > limit else all_events
                else:
                    page = all_events[:limit]

                # Resolve the session_id that was actually used for the response.
                # The helper uses the same fallback chain internally; the first
                # event's sid field is the ground truth when sid was None.
                resolved_sid: str = (
                    sid
                    if sid is not None
                    else (
                        page[0].get("sid", "unknown")
                        if page
                        else (
                            all_events[0].get("sid", "unknown")
                            if all_events
                            else "unknown"
                        )
                    )
                )

                result_obj = {
                    "session_id": resolved_sid,
                    "event_count": event_count,
                    "returned": len(page),
                    "skipped_lines": skipped_lines,
                    "events": page,
                }
                return ToolResult(output=json.dumps(result_obj, indent=2))

            elif operation == "garden":
                # On-demand deep analysis: cluster detection + KG enrichment.
                # Clamp max_drawers to [1, 500]; default 200.
                raw_max: int = int(kwargs.get("max_drawers", 200))
                max_drawers_clamped = max(1, min(raw_max, 500))

                # Total operation timeout: execute_garden is sync but makes many
                # MCP subprocess calls. Wrap in asyncio.to_thread so we can
                # enforce a hard wall-clock budget via asyncio.wait_for.
                #
                # NOTE: asyncio.wait_for cancels the Task, not the thread. The
                # underlying execute_garden thread continues running after
                # TimeoutError — worst-case ~37min of background _mcp_call
                # activity if the palace is slow. This is acceptable because:
                #   (a) each _mcp_call has a 15s per-call timeout bounding
                #       the worst-case background activity
                #   (b) garden is a non-interactive background operation —
                #       the caller gets a timely response and the thread will
                #       eventually complete on its own
                # Do NOT treat the 120s wall-clock budget as a hard resource
                # bound; treat it as a response-time guarantee to the caller.

                def combined_emit(
                    hook: str,
                    event: str,
                    *,
                    ok: bool = True,
                    preview: str | None = None,
                    data: dict[str, Any] | None = None,
                    session_id: str | None = None,
                ) -> None:
                    """Emit to private JSONL and forward to coordinator bridge."""
                    emit_event(
                        hook,
                        event,
                        ok=ok,
                        preview=preview,
                        data=data,
                        session_id=session_id,
                    )
                    try:
                        payload = {"ok": ok, "preview": preview, **(data or {})}
                        self._bridge_emit(f"memory-mempalace:{event}", payload)
                    except Exception:
                        pass

                try:
                    garden_result = await asyncio.wait_for(
                        asyncio.to_thread(
                            execute_garden,
                            wing=kwargs.get("wing"),
                            room=kwargs.get("room"),
                            lookback_days=int(kwargs.get("lookback_days", 90)),
                            max_drawers=max_drawers_clamped,
                            cluster_threshold=float(
                                kwargs.get("cluster_threshold", 0.80)
                            ),
                            emit_fn=combined_emit,
                            session_id=kwargs.get("session_id"),
                        ),
                        timeout=_GARDEN_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    # Emit garden_completed(ok=False) so observability tools see
                    # timed-out runs alongside successful ones.
                    try:
                        emit_event(
                            "tool-mempalace",
                            "garden_completed",
                            ok=False,
                            data={
                                "scope_wing": kwargs.get("wing"),
                                "scope_room": kwargs.get("room"),
                                "drawers_analyzed": 0,
                                "clusters_found": 0,
                                "kg_edges_created": 0,
                                "timed_out": True,
                            },
                            session_id=kwargs.get("session_id"),
                        )
                    except Exception:
                        pass  # never let event emission failure crash the error path
                    try:
                        self._bridge_emit(
                            "memory-mempalace:garden_completed",
                            {
                                "ok": False,
                                "scope_wing": kwargs.get("wing"),
                                "scope_room": kwargs.get("room"),
                                "drawers_analyzed": 0,
                                "clusters_found": 0,
                                "kg_edges_created": 0,
                                "timed_out": True,
                            },
                        )
                    except Exception:
                        pass
                    return ToolResult(
                        success=False,
                        error={
                            "message": (
                                f"Garden operation timed out (>{_GARDEN_TIMEOUT_S}s). "
                                "Consider reducing max_drawers or scoping to a specific room."
                            )
                        },
                    )

                # Emit garden_completed event (CP1 spec Section 2)
                emit_event(
                    "tool-mempalace",
                    "garden_completed",
                    ok=True,
                    data={
                        "scope_wing": kwargs.get("wing"),
                        "scope_room": kwargs.get("room"),
                        "drawers_analyzed": garden_result["drawers_analyzed"],
                        "clusters_found": len(garden_result["clusters"]),
                        "kg_edges_created": garden_result["kg_edges_created"],
                    },
                    session_id=kwargs.get("session_id"),
                )
                try:
                    self._bridge_emit(
                        "memory-mempalace:garden_completed",
                        {
                            "ok": True,
                            "scope_wing": kwargs.get("wing"),
                            "scope_room": kwargs.get("room"),
                            "drawers_analyzed": garden_result["drawers_analyzed"],
                            "clusters_found": len(garden_result["clusters"]),
                            "kg_edges_created": garden_result["kg_edges_created"],
                        },
                    )
                except Exception:
                    pass

                return ToolResult(output=json.dumps(garden_result, indent=2))

            else:
                return ToolResult(
                    success=False,
                    error={"message": f"Unknown operation: {operation}"},
                )

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False, error={"message": "MemPalace operation timed out."}
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error={
                    "message": (
                        "MemPalace CLI not found. Install with: pip install mempalace\n"
                        "Then initialize a palace: mempalace init <path>"
                    )
                },
            )
        except Exception as exc:
            return ToolResult(success=False, error={"message": f"Error: {exc}"})


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the palace tool into the Amplifier coordinator."""
    cfg = config or {}

    register_events(
        coordinator,
        "memory-mempalace-tool",
        ["memory-mempalace:garden_completed", "memory-mempalace:garden_progress"],
    )

    # Optional "running shadow" for kg add/invalidate + diary write — same
    # shadow_gateway config schema as hooks-mempalace-capture. Off by default.
    _configure_shadow(cfg.get("shadow_gateway") or {})

    bridge_emit = make_sync_bridge(coordinator)
    tool = PalaceTool(bridge_emit=bridge_emit)
    await coordinator.mount("tools", tool, name=tool.name)
    return {
        "name": "tool-mempalace",
        "version": "1.2.0",
        "provides": ["palace"],
    }
