"""
load_captures — first node of the curate.dot pipeline.

Reads the drawers filed during a session from the MemPalace event log
(``~/.mempalace/events/{session_id}.jsonl``) and emits them as a JSON list on
stdout for the downstream consolidation nodes.

The event log is the durable, dependency-free record of what was filed; it is
written by the capture hook's event emitter. (Full verbatim drawer content for
a richer consolidation pass requires a palace query — that is a deliberate
follow-on; the metadata + preview here is enough to dedup and route.)

CLI:
    mempalace-load-captures <session_id> [events_root]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_FIELDS = ("capture_id", "wing", "room", "category", "content_bytes", "source")


def _default_events_root() -> Path:
    return Path.home() / ".mempalace" / "events"


def load_captures(
    session_id: str, events_root: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return the list of drawers filed during ``session_id``.

    Reads ``{events_root}/{session_id}.jsonl`` and keeps only ``drawer_filed``
    events. Malformed lines are skipped. Missing file -> empty list.
    """
    root = Path(events_root) if events_root is not None else _default_events_root()
    path = root / f"{session_id}.jsonl"
    if not path.is_file():
        return []

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("event") != "drawer_filed":
            continue
        data = event.get("data") or {}
        row: dict[str, Any] = {k: data.get(k) for k in _FIELDS}
        row["preview"] = event.get("preview")
        row["ts"] = event.get("ts")
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("usage: mempalace-load-captures <session_id> [events_root]\n")
        return 2
    session_id = args[0]
    events_root = args[1] if len(args) > 1 else None
    rows = load_captures(session_id, events_root)
    sys.stdout.write(json.dumps(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
