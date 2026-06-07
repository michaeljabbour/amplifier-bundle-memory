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
from typing import Any, Protocol, runtime_checkable


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
    """Writes consolidated cells into the amplifier-data event-log substrate.

    Mapping onto amplifier-data's API (verified against the real library,
    docs/CONSUMER_INTEGRATION.md §3):
      - drawer content -> ``write_cell(bytes)`` (verbatim, content-addressed; E1)
      - wing/room      -> ``scope(ref, scope_cell)`` (``scoped_to`` edges;
                          hierarchy = multiple scope edges per drawer)
      - category/importance/source -> ``assert_fact(ref, predicate, object_cell)``
                          (queryable, invalidatable KG facts; the object of a
                          fact is itself a content-addressed cell)

    amplifier-data is an OPTIONAL dependency. Construction raises loudly if it
    is not installed — never a silent no-op.
    """

    def __init__(
        self,
        store: object | None = None,
        *,
        path: str | None = None,
        base_url: str | None = None,
        token: str | None = None,
        record_access: bool = False,
    ) -> None:
        if store is None:
            if base_url is not None and token is not None:
                # Authed MCP gateway: token-protected, single-writer, MCP-shaped.
                from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (
                    GatewayClient,
                )

                store = GatewayClient(base_url, token)
            elif base_url is not None:
                # Native companion server (localhost, no auth): funnel writes
                # through the single-writer server so multiple processes share
                # one palace safely.
                try:
                    from amplifier_data.client import RemoteStore
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError(
                        "AmplifierDataMemoryStore(base_url=...) requires amplifier-data."
                    ) from exc
                store = RemoteStore(base_url)
            else:
                try:
                    from amplifier_data import AmplifierStore
                except ImportError as exc:  # pragma: no cover - exercised when absent
                    raise RuntimeError(
                        "AmplifierDataMemoryStore requires the amplifier-data package, "
                        "which is not installed. Install it (pip install -e amplifier-data) "
                        "or use PalaceMemoryStore."
                    ) from exc
                # record_access=False by default: consolidation is a write path;
                # we do not want every later read to append AccessEvents (§5).
                store = AmplifierStore(path=path, record_access=record_access)
        self.store: Any = store
        self.filed: list[dict[str, object]] = []

    def close(self) -> None:
        """Close the backing store if it supports it (RemoteStore does not)."""
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

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
        s = self.store
        ref = s.write_cell(content.encode("utf-8"))  # type: ignore[attr-defined]
        # wing/room scoping — content-addressed scope cells (idempotent refs).
        s.scope(ref, s.write_cell(f"wing:{wing}".encode()))  # type: ignore[attr-defined]
        s.scope(ref, s.write_cell(f"room:{room}".encode()))  # type: ignore[attr-defined]
        # queryable KG facts; a fact's object must itself be a cell ref.
        if source:
            s.assert_fact(ref, "has_source", s.write_cell(source.encode()))  # type: ignore[attr-defined]
        if category is not None:
            s.assert_fact(  # type: ignore[attr-defined]
                ref, "has_category", s.write_cell(str(category).encode())
            )
        if importance is not None:
            s.assert_fact(  # type: ignore[attr-defined]
                ref, "has_importance", s.write_cell(str(importance).encode())
            )
        self.filed.append(
            {
                "ref": ref,
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "importance": importance,
            }
        )
        return ref


class DualWriteMemoryStore:
    """Migration fan-out: write to a primary (source of truth) and a shadow.

    The primary stays authoritative (the palace today); the shadow
    (amplifier-data) is written in parallel so regenerated views can be compared
    against the primary. A shadow failure NEVER breaks the primary write — it is
    recorded in ``shadow_errors`` (set ``fail_on_shadow_error=True`` to surface
    it). This is the §8 dual-write migration pattern.
    """

    def __init__(
        self,
        primary: MemoryStore,
        shadow: MemoryStore,
        *,
        fail_on_shadow_error: bool = False,
    ) -> None:
        self.primary = primary
        self.shadow = shadow
        self.fail_on_shadow_error = fail_on_shadow_error
        self.shadow_errors: list[str] = []

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
        self.primary.file(
            wing=wing,
            room=room,
            content=content,
            source=source,
            category=category,
            importance=importance,
        )
        try:
            self.shadow.file(
                wing=wing,
                room=room,
                content=content,
                source=source,
                category=category,
                importance=importance,
            )
        except Exception as exc:  # shadow must never break the source of truth
            self.shadow_errors.append(f"{type(exc).__name__}: {exc}")
            if self.fail_on_shadow_error:
                raise
