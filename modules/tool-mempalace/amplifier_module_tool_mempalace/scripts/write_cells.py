"""
write_cells — final node of the curate.dot pipeline.

Reads consolidated cells (JSON list) and files each one through a MemoryStore.
Each cell is a dict with at least ``wing``, ``room``, and ``content``; optional
``source``, ``category``, and ``importance``.

CLI:
    mempalace-write-cells [cells.json]
        Reads cells from the given file, or from stdin when omitted.
        Files them via PalaceMemoryStore and prints the count written.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from amplifier_module_tool_mempalace.scripts.memory_store import (
    MemoryStore,
    PalaceMemoryStore,
)


def write_cells(cells: list[dict[str, Any]], store: MemoryStore) -> int:
    """File each consolidated cell through ``store``. Returns the count written."""
    count = 0
    for cell in cells:
        store.file(
            wing=cell["wing"],
            room=cell["room"],
            content=cell["content"],
            source=cell.get("source", ""),
            category=cell.get("category"),
            importance=cell.get("importance"),
        )
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    raw = (
        open(args[0], encoding="utf-8").read()  # noqa: SIM115 - short-lived CLI read
        if args
        else sys.stdin.read()
    )
    try:
        cells = json.loads(raw) if raw.strip() else []
    except ValueError:
        sys.stderr.write("write_cells: input is not valid JSON\n")
        return 2
    if not isinstance(cells, list):
        sys.stderr.write("write_cells: expected a JSON list of cells\n")
        return 2

    n = write_cells(cells, PalaceMemoryStore())
    sys.stdout.write(str(n))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
