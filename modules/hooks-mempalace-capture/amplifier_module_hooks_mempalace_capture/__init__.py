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

Credits: MemPalace (github.com/MemPalace/mempalace).
"""

from __future__ import annotations

import os
import subprocess
import json
from pathlib import Path
from typing import Any

try:
    from amplifier_core import Hook, HookContext, mount_hook  # type: ignore
except ImportError:
    # Graceful degradation when running outside Amplifier (e.g., tests)
    class Hook:  # type: ignore
        name: str = ""
        events: list[str] = []

        def __init__(self, config: dict[str, Any] | None = None) -> None:
            pass

    class HookContext:  # type: ignore
        def __init__(self, event: dict[str, Any] | None = None) -> None:
            self.event: dict[str, Any] = event or {}
            self.session_id: str | None = None

        def inject_context(self, content: str, *, ephemeral: bool = True) -> None:
            pass

    def mount_hook(*args: Any, **kwargs: Any) -> None:  # type: ignore
        pass


try:
    from amplifier_module_tool_mempalace.event_emitter import (
        emit_event,
        truncate_preview,
    )
except ImportError:

    def emit_event(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass

    def truncate_preview(text: Any) -> Any:  # type: ignore[misc]
        if text is None:
            return None
        return text[:97] + "..." if len(text) > 100 else text


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


def _detect_category(text: str) -> str | None:
    """Heuristically detect a memory category from text content."""
    lower = text.lower()
    for category, signals in _CATEGORY_SIGNALS.items():
        if any(signal in lower for signal in signals):
            return category
    return None


class MempalaceCaptureHook(Hook):
    name = "hooks-mempalace-capture"
    events = ["tool:post"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.auto_wing: bool = self.config.get("auto_wing", True)
        self.auto_room: bool = self.config.get("auto_room", True)
        self.silent: bool = self.config.get("silent", True)
        self.emit_events: bool = bool(self.config.get("emit_events", True))
        # Categories to capture (empty list = capture all palace-worthy content)
        self.categories: list[str] = self.config.get("categories", [])

    async def handle(self, ctx: HookContext) -> None:  # type: ignore[override]
        tool_name: str = ctx.event.get("tool_name", "unknown")
        tool_input: dict[str, Any] = ctx.event.get("tool_input", {})
        tool_output: str = str(ctx.event.get("tool_output", ""))

        sid = getattr(ctx, "session_id", None) or ctx.event.get("session_id")

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
            return

        # Category detection (merged from hooks-memory-capture)
        category = _detect_category(tool_output)
        if self.categories and category not in self.categories:
            # Category filter active but this output doesn't match — skip
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output),
                    data={"reason": "category_filtered"},
                    session_id=sid,
                )
            return

        wing = (
            _detect_wing()
            if self.auto_wing
            else self.config.get("wing", "wing_general")
        )
        base_room = (
            _detect_room(tool_name, tool_input)
            if self.auto_room
            else self.config.get("room", "general")
        )
        # Enrich room name with category if detected
        room = f"{base_room}-{category}" if category else base_room
        source = tool_input.get("path", tool_input.get("file_path", tool_name))

        try:
            _mcp_add_drawer(wing, room, tool_output, str(source))
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "drawer_filed",
                    ok=True,
                    preview=truncate_preview(tool_output),
                    data={
                        "wing": wing,
                        "room": room,
                        "category": category,
                        "content_bytes": len(tool_output.encode("utf-8")),
                        "source": str(source),
                        # Dedup status is determined by Curator Phase 3, not at capture time.
                    },
                    session_id=sid,
                )
        except Exception:
            if self.emit_events:
                emit_event(
                    "mempalace-capture",
                    "capture_skipped",
                    ok=False,
                    preview=truncate_preview(tool_output),
                    data={"reason": "mcp_error"},
                    session_id=sid,
                )
            if not self.silent:
                raise


def mount() -> list[Hook]:
    """Amplifier module entry point."""
    return [MempalaceCaptureHook()]
