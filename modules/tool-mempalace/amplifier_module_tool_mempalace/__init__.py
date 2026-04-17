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

All 29 raw MCP tools are also available via the mempalace_* prefix
through the MCP integration in the bundle behavior.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from amplifier_core import Tool, ToolResult  # type: ignore


PALACE_PATH = Path.home() / ".mempalace"


def _mcp_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a MemPalace MCP tool via the CLI and return the result."""
    payload = json.dumps({"tool": tool_name, "arguments": args})
    result = subprocess.run(
        ["mempalace", "mcp", "--call", payload],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or "MCP call failed"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"result": result.stdout.strip()}


class PalaceTool(Tool):
    name = "palace"
    description = (
        "MemPalace memory operations. Operations: search, remember, status, "
        "kg (knowledge graph), traverse, diary, mine."
    )
    parameters = {
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
                "description": "Max results to return (default: 5).",
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
        },
        "required": ["operation"],
    }

    async def execute(self, operation: str, **kwargs: Any) -> ToolResult:  # type: ignore[override]
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
                return ToolResult(content=json.dumps(result, indent=2))

            elif operation == "remember":
                args = {
                    "wing": kwargs.get("wing", "general"),
                    "room": kwargs.get("room", "notes"),
                    "content": kwargs.get("content", ""),
                    "added_by": "amplifier",
                }
                result = _mcp_call("mempalace_add_drawer", args)
                return ToolResult(content=json.dumps(result, indent=2))

            elif operation == "status":
                result = _mcp_call("mempalace_status", {})
                return ToolResult(content=json.dumps(result, indent=2))

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
                elif kg_action == "invalidate":
                    args = {
                        "subject": kwargs.get("subject", ""),
                        "predicate": kwargs.get("predicate", ""),
                        "object": kwargs.get("object", ""),
                    }
                    result = _mcp_call("mempalace_kg_invalidate", args)
                elif kg_action == "timeline":
                    args = {"entity": kwargs.get("entity", "")}
                    result = _mcp_call("mempalace_kg_timeline", args)
                else:  # stats
                    result = _mcp_call("mempalace_kg_stats", {})
                return ToolResult(content=json.dumps(result, indent=2))

            elif operation == "traverse":
                args = {
                    "start_room": kwargs.get("start_room", ""),
                    "max_hops": kwargs.get("max_hops", 2),
                }
                result = _mcp_call("mempalace_traverse", args)
                return ToolResult(content=json.dumps(result, indent=2))

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
                else:
                    args = {"agent_name": agent_name, "last_n": kwargs.get("limit", 10)}
                    result = _mcp_call("mempalace_diary_read", args)
                return ToolResult(content=json.dumps(result, indent=2))

            elif operation == "mine":
                path = kwargs.get("path", ".")
                mode = kwargs.get("mode", "files")
                proc = subprocess.run(
                    ["mempalace", "mine", path, "--mode", mode],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                output = proc.stdout + proc.stderr
                return ToolResult(content=output.strip())

            else:
                return ToolResult(
                    content=f"Unknown operation: {operation}", is_error=True
                )

        except subprocess.TimeoutExpired:
            return ToolResult(content="MemPalace operation timed out.", is_error=True)
        except FileNotFoundError:
            return ToolResult(
                content=(
                    "MemPalace CLI not found. Install with: pip install mempalace\n"
                    "Then initialize a palace: mempalace init <path>"
                ),
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)


def mount() -> list[Tool]:
    """Amplifier module entry point."""
    return [PalaceTool()]
