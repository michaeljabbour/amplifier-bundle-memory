"""
MemoryStore — the storage seam for the consolidation pipeline.

The cold-path ``curate.dot`` pipeline produces consolidated "cells" and writes
them through a ``MemoryStore``. Today the only real backend is the MemPalace
drawer store (``PalaceMemoryStore``). The amplifier-data substrate is a declared
seam (``AmplifierDataMemoryStore``) that fails loudly until persistence + a
vector lens land in amplifier-data — it must never silently pretend to store.

Keeping this as a one-method protocol means the pipeline's ``write_cells`` node
is identical regardless of backend; only the injected store changes.
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryStore(Protocol):
    """A sink for consolidated memory cells."""

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        """Persist one consolidated cell."""
        ...


class RecordingMemoryStore:
    """In-memory store for tests and pipeline dry-runs.

    Records every cell instead of persisting it, so the pipeline data path can
    be exercised end-to-end with no MemPalace dependency.
    """

    def __init__(self) -> None:
        self.filed: list[dict[str, object]] = []

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        self.filed.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
            }
        )


class PalaceMemoryStore:
    """Writes consolidated cells as verbatim MemPalace drawers via the CLI.

    Mirrors the capture hook's ``_mcp_add_drawer`` call so consolidated content
    lands in the palace exactly like raw captures do.
    """

    def __init__(self, added_by: str = "curate-pipeline") -> None:
        self.added_by = added_by

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "tool": "mempalace_add_drawer",
                "arguments": {
                    "wing": wing,
                    "room": room,
                    "content": content,
                    "source_file": source,
                    "added_by": self.added_by,
                },
            }
        )
        subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True,
            text=True,
            timeout=15,
        )


class AmplifierDataMemoryStore:
    """Phase 3 seam: the amplifier-data substrate (event-log + lenses).

    Not wired yet — amplifier-data is blocked on persistence and a vector lens.
    This stub exists so the pipeline can target the substrate by configuration
    once those land, and so the gap is explicit and loud rather than silent.
    """

    def file(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str = "",
        category: str | None = None,
        importance: float | None = None,
    ) -> None:
        raise NotImplementedError(
            "AmplifierDataMemoryStore is not wired: amplifier-data needs "
            "persistence + a vector lens before it can back memory. "
            "Use PalaceMemoryStore for now."
        )
