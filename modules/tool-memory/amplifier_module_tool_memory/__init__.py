"""
amplifier-module-tool-memory
High-level Amplifier tool wrapping the native amplifier-data memory store.

Provides a single `memory` tool with sub-operations:
  search    — Semantic search (scoped by wing/room)
  remember  — File verbatim content as a drawer
  status    — Memory store overview
  kg        — Knowledge graph query/add/invalidate
  traverse  — Graph traversal across wings
  diary     — Agent diary read/write
  mine      — Mine a directory or conversation file
  events    — Query the per-session JSONL event log (CP2)
  garden    — On-demand cluster analysis

Native cutover (docs/plans/2026-07-07-native-cutover-design.md): every
operation routes through ``MemoryClient`` via ``ensure_daemon()`` against the
auto-started memory daemon -- there is no vendor subprocess anywhere in this
module. The daemon IS the store (not a shadow of one). Tool name is `memory`
(operations unchanged).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from amplifier_core import Tool, ToolResult  # type: ignore

from .client import ensure_daemon
from .coordinator_bridge import (
    NOOP_SYNC_BRIDGE,
    SyncBridge,
    make_sync_bridge,
    register_events,
)
from .event_emitter import _read_events_with_skip_count, emit_event
from .garden import execute_garden

# Hard wall-clock budget for garden operations. Patchable in tests.
_GARDEN_TIMEOUT_S: float = 120.0

# ---------------------------------------------------------------------------
# Native transport seam (B2): the ONE place every operation branch below
# calls through to the auto-started memory daemon via MemoryClient.
# ---------------------------------------------------------------------------


def _call_client(method: str, **kwargs: Any) -> Any:
    """Invoke ``MemoryClient.<method>(**kwargs)`` against the native memory
    daemon (``ensure_daemon()``, \u00a75.2 of the native-cutover design).

    This is the ONE transport seam every ``MemoryTool`` operation routes
    through -- it replaces the old vendor subprocess transport entirely.
    Raises ``RuntimeError("memory daemon unavailable")`` when
    ``ensure_daemon()`` cannot reach or spawn a daemon (\u00a75.7's client
    degradation contract: tool ops -> loud ``ToolResult(success=False)``);
    a genuine client-side exception (e.g. a malformed argument rejected by
    the daemon) propagates as-is. Both are caught by each operation's own
    try/except and converted into a failure ``ToolResult`` -- never raised
    into the caller.
    """
    client = ensure_daemon()
    if client is None:
        raise RuntimeError("memory daemon unavailable")
    return getattr(client, method)(**kwargs)


def _client_result_to_tool_result(
    result: Any, *, wrap_key: str | None = None
) -> ToolResult:
    """Success path: wrap a ``MemoryClient`` method's return value into a
    contract-correct ``ToolResult(success=True, output=<json>)``.

    ``MemoryClient`` domain methods return typed values (``str`` refs,
    ``list`` results, ``dict`` payloads) rather than the old MCP transport's
    uniform ``{"error": ...}``-or-passthrough dict -- ``wrap_key`` lets a
    scalar/list result be nested under a stable key (e.g. ``{"ref": "..."}``)
    so the JSON output shape stays predictable across operations.
    """
    payload = result if wrap_key is None else {wrap_key: result}
    return ToolResult(success=True, output=json.dumps(payload, indent=2))


def _client_error_to_tool_result(exc: Exception) -> ToolResult:
    """Failure path: any exception from :func:`_call_client` (daemon
    unavailable, or a genuine client/daemon-side error) becomes a loud,
    contract-correct ``ToolResult(success=False, error={...})`` -- never a
    silent success, matching the store-missing failure mode this tool
    always had."""
    return ToolResult(success=False, output=None, error={"message": str(exc)})


# ---------------------------------------------------------------------------
# `mine` operation: native, in-process file/conversation walker.
#
# Replaces the old CLI shell-out entirely -- there is no vendor CLI to shell
# out to anymore. `files` mode
# walks a directory (or a single file) for text/code files and chunks them;
# `convos` mode parses conversation exports (JSON array or JSONL, one message
# object per line -- the shape amplifier's own transcript.jsonl uses) and
# chunks per message. Both modes file chunks via `client.remember(...)`, the
# SAME native write path `remember` uses -- there is no separate "mine store".
# ---------------------------------------------------------------------------

#: File extensions considered for `mine` `files` mode. Deliberately broad but
#: bounded -- text/markdown/code, not binaries.
_MINE_FILE_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".rst",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".sh",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
    }
)
_MINE_CHUNK_CHARS = 2000
_MINE_MAX_FILES = 500


def _iter_mine_files(root: Path) -> Iterator[Path]:
    """Yield files under *root* eligible for `mine` `files` mode.

    A single file path yields itself regardless of extension (explicit
    request). A directory is walked recursively, filtered by
    ``_MINE_FILE_EXTENSIONS`` and skipping any path with a dotfile/dotdir
    component (``.git``, ``.venv``, etc.) -- deterministic order (sorted).
    """
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _MINE_FILE_EXTENSIONS:
            continue
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:  # pragma: no cover - defensive
            rel_parts = p.parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        yield p


def _chunk_text(text: str, max_chars: int = _MINE_CHUNK_CHARS) -> list[str]:
    """Chunk *text* on paragraph (blank-line) boundaries, each chunk bounded
    by *max_chars*. An overlong single paragraph is hard-split. Never raises;
    empty/whitespace-only input yields no chunks."""
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current.strip():
            chunks.append(current)
        if len(para) > max_chars:
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
            current = ""
        else:
            current = para
    if current.strip():
        chunks.append(current)
    return chunks


def _mine_files(path: str) -> dict[str, Any]:
    """`files` mode: walk *path*, chunk each eligible file, file every chunk
    verbatim via ``client.remember`` under ``wing_<root-name>`` / room
    ``mine-files``. Returns a summary dict; never raises for a per-file read
    error (recorded in ``errors``) -- only a missing root path or a daemon
    failure raises (converted to a failure ToolResult by the caller)."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"mine path does not exist: {path}")

    wing = f"wing_{(root.resolve().name or 'root')}"
    files_scanned = 0
    chunks_filed = 0
    errors: list[str] = []

    for file_path in _iter_mine_files(root):
        if files_scanned >= _MINE_MAX_FILES:
            break
        files_scanned += 1
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            errors.append(f"{file_path}: {exc}")
            continue
        rel_source = (
            str(file_path.relative_to(root)) if root.is_dir() else file_path.name
        )
        for chunk in _chunk_text(text):
            _call_client(
                "remember",
                wing=wing,
                room="mine-files",
                content=chunk,
                source=rel_source,
            )
            chunks_filed += 1

    return {
        "mode": "files",
        "path": str(root),
        "files_scanned": files_scanned,
        "chunks_filed": chunks_filed,
        "errors": errors,
    }


def _parse_convo_records(raw: str) -> list[dict[str, Any]]:
    """Parse a conversation export as either a JSON array/object or JSONL
    (one message object per line -- amplifier's own transcript.jsonl shape).
    Malformed lines are skipped; never raises."""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _mine_convos(path: str) -> dict[str, Any]:
    """`convos` mode: parse conversation export file(s) under *path* and file
    each message chunk via ``client.remember`` under ``wing_<parent-name>`` /
    room ``mine-convos``. Best-effort: an unparseable file contributes to
    ``errors`` and is otherwise skipped, never raises."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"mine path does not exist: {path}")

    files = [root] if root.is_file() else sorted(root.rglob("*.json*"))
    wing_source = root if root.is_dir() else root.parent
    wing = f"wing_{(wing_source.resolve().name or 'root')}"
    files_scanned = 0
    messages_filed = 0
    errors: list[str] = []

    for file_path in files:
        files_scanned += 1
        try:
            raw = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            errors.append(f"{file_path}: {exc}")
            continue

        for rec in _parse_convo_records(raw):
            role = str(rec.get("role") or rec.get("speaker") or "unknown")
            content = rec.get("content") or rec.get("text") or ""
            if isinstance(content, list):
                content = "\n".join(str(c) for c in content)
            content = str(content).strip()
            if not content:
                continue
            for chunk in _chunk_text(content):
                _call_client(
                    "remember",
                    wing=wing,
                    room="mine-convos",
                    content=chunk,
                    source=f"{file_path.name}:{role}",
                )
                messages_filed += 1

    return {
        "mode": "convos",
        "path": str(root),
        "files_scanned": files_scanned,
        "messages_filed": messages_filed,
        "errors": errors,
    }


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Memory operations. Operations: search, remember, status, "
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
                "description": "The memory operation to perform.",
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
                "description": (
                    "Mining mode: 'files' for project files, 'convos' for conversation exports."
                ),
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
                    "(e.g. 'memory-capture'). For 'events' operation."
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
                    "Only analyze drawers added in the last N days (garden operation). Default: 90."
                ),
                "default": 90,
            },
            "max_drawers": {
                "type": "integer",
                "description": (
                    "Budget cap: max drawers to analyze per garden run. Default 200, hard cap 500."
                ),
                "default": 200,
            },
            "cluster_threshold": {
                "type": "number",
                "description": (
                    "Cosine similarity threshold for clustering (garden operation). Default: 0.80."
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
                try:
                    result = _call_client(
                        "search",
                        query=kwargs.get("query", ""),
                        k=int(kwargs.get("limit", 5)),
                        wing=kwargs.get("wing") or None,
                        room=kwargs.get("room") or None,
                    )
                except Exception as exc:
                    return _client_error_to_tool_result(exc)
                return _client_result_to_tool_result(result)

            elif operation == "remember":
                try:
                    ref = _call_client(
                        "remember",
                        wing=kwargs.get("wing", "general"),
                        room=kwargs.get("room", "notes"),
                        content=kwargs.get("content", ""),
                        source=kwargs.get("source", "") or "",
                    )
                except Exception as exc:
                    return _client_error_to_tool_result(exc)
                return _client_result_to_tool_result(ref, wrap_key="ref")

            elif operation == "status":
                try:
                    result = _call_client("status")
                except Exception as exc:
                    return _client_error_to_tool_result(exc)
                return _client_result_to_tool_result(result)

            elif operation == "kg":
                kg_action = kwargs.get("kg_action", "query")
                try:
                    if kg_action == "query":
                        facts = _call_client(
                            "kg_query",
                            subject=kwargs.get("entity") or None,
                            predicate=None,
                        )
                        result: Any = {"facts": [list(f) for f in facts]}
                    elif kg_action == "add":
                        _call_client(
                            "kg_add",
                            subject=kwargs.get("subject", ""),
                            predicate=kwargs.get("predicate", ""),
                            object=kwargs.get("object", ""),
                        )
                        result = {"ok": True}
                    elif kg_action == "invalidate":
                        _call_client(
                            "kg_invalidate",
                            subject=kwargs.get("subject", ""),
                            predicate=kwargs.get("predicate", ""),
                            object=kwargs.get("object", ""),
                        )
                        result = {"ok": True}
                    elif kg_action == "timeline":
                        entries = _call_client(
                            "kg_timeline", subject=kwargs.get("entity", "")
                        )
                        result = {"entries": entries}
                    else:  # stats
                        result = _call_client("kg_stats")
                except Exception as exc:
                    return _client_error_to_tool_result(exc)
                return _client_result_to_tool_result(result)

            elif operation == "traverse":
                try:
                    refs = _call_client(
                        "traverse",
                        start=kwargs.get("start_room", ""),
                        max_hops=int(kwargs.get("max_hops", 2)),
                    )
                except Exception as exc:
                    return _client_error_to_tool_result(exc)
                return _client_result_to_tool_result(refs, wrap_key="refs")

            elif operation == "diary":
                diary_action = kwargs.get("diary_action", "read")
                agent_name = kwargs.get("agent_name", "amplifier")
                try:
                    if diary_action == "write":
                        ref = _call_client(
                            "diary_write",
                            agent_name=agent_name,
                            entry=kwargs.get("entry", ""),
                            topic=kwargs.get("room", "general"),
                        )
                        return _client_result_to_tool_result(ref, wrap_key="ref")
                    entries = _call_client(
                        "diary_read",
                        agent_name=agent_name,
                        last_n=int(kwargs.get("limit", 10)),
                    )
                    return _client_result_to_tool_result(entries, wrap_key="entries")
                except Exception as exc:
                    return _client_error_to_tool_result(exc)

            elif operation == "mine":
                path = kwargs.get("path", ".")
                mode = kwargs.get("mode", "files")
                try:
                    mine_result = (
                        _mine_convos(path) if mode == "convos" else _mine_files(path)
                    )
                except FileNotFoundError as exc:
                    return ToolResult(success=False, error={"message": str(exc)})
                except Exception as exc:
                    return ToolResult(
                        success=False, error={"message": f"mine failed: {exc}"}
                    )
                return ToolResult(
                    success=True, output=json.dumps(mine_result, indent=2)
                )

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
                # daemon calls. Wrap in asyncio.to_thread so we can enforce a
                # hard wall-clock budget via asyncio.wait_for.
                #
                # NOTE: asyncio.wait_for cancels the Task, not the thread. The
                # underlying execute_garden thread continues running after
                # TimeoutError. This is acceptable because garden is a
                # non-interactive background operation -- the caller gets a
                # timely response and the thread will eventually complete
                # on its own. Do NOT treat the 120s wall-clock budget as a
                # hard resource bound; treat it as a response-time guarantee.

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
                        self._bridge_emit(f"memory:{event}", payload)
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
                except TimeoutError:
                    # Emit garden_completed(ok=False) so observability tools see
                    # timed-out runs alongside successful ones.
                    try:
                        emit_event(
                            "tool-memory",
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
                            "memory:garden_completed",
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
                    "tool-memory",
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
                        "memory:garden_completed",
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

        except Exception as exc:
            return ToolResult(success=False, error={"message": f"Error: {exc}"})


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the memory tool into the Amplifier coordinator."""
    register_events(
        coordinator,
        "memory-tool",
        ["memory:garden_completed", "memory:garden_progress"],
    )

    bridge_emit = make_sync_bridge(coordinator)
    tool = MemoryTool(bridge_emit=bridge_emit)
    await coordinator.mount("tools", tool, name=tool.name)
    return {
        "name": "tool-memory",
        "version": "2.0.0",
        "provides": ["memory"],
    }
