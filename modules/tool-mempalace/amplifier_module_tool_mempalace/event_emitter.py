"""
Shared JSONL event emitter for all amplifier-bundle-memory hooks.

All hooks import from here:
    from amplifier_module_tool_mempalace.event_emitter import emit_event, truncate_preview

Events are written to: ~/.mempalace/events/{session_id}.jsonl

Thread-safe. Never raises — errors are silently swallowed so hooks are
never disrupted by observability failures.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# Cached session ID — resolved once, reused for the lifetime of the module.
# Tests can monkeypatch this to None to reset state between calls.
_cached_session_id: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers (patchable for tests)
# ---------------------------------------------------------------------------


def _mempalace_base() -> Path | None:
    """Return ~/.mempalace if it exists, else None.

    Tests can monkeypatch this function to redirect I/O to a temp directory.
    If ~/.mempalace/ doesn't exist (MemPalace not initialised), we return None
    and emit_event becomes a silent no-op — we never create the parent dir.
    """
    mp = Path.home() / ".mempalace"
    if not mp.exists():
        return None
    return mp


def _resolve_session_id(session_id: str | None = None) -> str:
    """Resolve a session ID using the fallback chain.

    Priority:
    1. Explicit ``session_id`` argument.
    2. ``AMPLIFIER_SESSION_ID`` environment variable.
    3. ``pid_{pid}_{YYYY-MM-DD}`` fallback (cached at module level).
    """
    global _cached_session_id

    if session_id is not None:
        return session_id

    env_sid = os.environ.get("AMPLIFIER_SESSION_ID")
    if env_sid:
        return env_sid

    # Fallback: pid-based session ID, cached for the module lifetime.
    if _cached_session_id is None:
        _cached_session_id = f"pid_{os.getpid()}_{date.today().isoformat()}"
    return _cached_session_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def truncate_preview(text: str | None) -> str | None:
    """Apply the canonical preview truncation rules (Section 2 of spec).

    Rules (in priority order):
    1. None → None.
    2. Not valid UTF-8 (bytes) → "[binary, N bytes]". (Strings are always
       valid Unicode in Python, so this only applies when the caller passes
       bytes via a broader type annotation — defensive check.)
    3. Contains a newline before position 100 → truncate at newline + "...".
    4. Length > 100 → first 97 chars + "...".
    5. Length ≤ 100 → use as-is.
    """
    if text is None:
        return None

    # Defensive: handle bytes-like objects that slipped through
    if isinstance(text, (bytes, bytearray)):
        return f"[binary, {len(text)} bytes]"

    # Rule 3: newline before position 100
    nl_pos = text.find("\n")
    if nl_pos != -1 and nl_pos < 100:
        return text[:nl_pos] + "..."

    # Rule 4: length > 100
    if len(text) > 100:
        return text[:97] + "..."

    # Rule 5: ≤ 100 chars, no newline before 100
    return text


def emit_event(
    hook: str,
    event: str,
    *,
    ok: bool = True,
    preview: str | None = None,
    data: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Append a structured event to the session's JSONL event log.

    Thread-safe. Never raises — all errors are silently swallowed to avoid
    disrupting the hook that called us.

    Args:
        hook: Emitting hook name (e.g. "mempalace-capture").
        event: Event type (e.g. "drawer_filed").
        ok: True if the operation succeeded, False on skip/error.
        preview: Optional short preview of the primary content (≤100 chars).
                 Pass the raw text — truncation is applied automatically.
        data: Event-specific structured payload. Defaults to {}.
        session_id: Explicit session ID. Falls back to env var then pid-based.
    """
    try:
        base = _mempalace_base()
        if base is None:
            # MemPalace not initialised — silent no-op.
            return

        sid = _resolve_session_id(session_id)

        # Build the record
        record: dict[str, Any] = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(),
            "sid": sid,
            "hook": hook,
            "event": event,
            "ok": ok,
            "preview": truncate_preview(preview),
            "data": data if data is not None else {},
        }

        line = json.dumps(record, separators=(",", ":")) + "\n"

        # Ensure the events directory exists (mkdir -p equivalent).
        events_dir = base / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        events_file = events_dir / f"{sid}.jsonl"

        with _lock:
            with events_file.open(mode="a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()

    except Exception:
        # Never crash a hook.
        pass


def read_events(
    session_id: str | None = None,
    *,
    hook_filter: str | None = None,
    event_filter: str | None = None,
    limit: int = 200,
    tail: bool = False,
) -> list[dict[str, Any]]:
    """Read events from a session's JSONL file.

    Returns a list of parsed event dicts. If ``tail=True``, returns the last
    ``limit`` events. Otherwise returns the first ``limit`` events.

    Returns an empty list if the file does not exist or cannot be read.
    """
    base = _mempalace_base()
    if base is None:
        return []

    sid = _resolve_session_id(session_id)
    events_file = base / "events" / f"{sid}.jsonl"

    if not events_file.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        for raw_line in events_file.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue  # skip corrupt lines

            # Apply filters
            if hook_filter and record.get("hook") != hook_filter:
                continue
            if event_filter and record.get("event") != event_filter:
                continue

            records.append(record)
    except Exception:
        return []

    if tail:
        return records[-limit:] if len(records) > limit else records
    return records[:limit]
