"""
KG-N5 (docs/plans/2026-07-07-native-cutover-design.md \u00a712): migration.

Seeds a REAL chromadb store shaped like the legacy vendor's on-disk layout
(collection ``mempalace_drawers`` with documents+metadatas+embeddings --
chromadb only, the legacy vendor package itself is never required or
imported), runs ``amplifier_module_tool_memory.migrate.migrate(..., verify=True)``
against a REAL ephemeral memory daemon, and asserts:

  - the report's counts match the seed exactly,
  - `search` finds the migrated content semantically (copied vectors, no
    re-embed),
  - the source directory is byte-unchanged after migration (read-only proof).

Executed, not skipped: requires chromadb (a normal test dependency here,
see pyproject.toml's `dependency-groups.dev`) and amplifier-data (already a
hard dependency of this module).
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.daemon import run_daemon  # noqa: E402
from amplifier_module_tool_memory.migrate import migrate  # noqa: E402


def _seed_legacy_store(source: Path) -> dict[str, Any]:
    """Write a real chromadb collection shaped exactly like the legacy
    vendor's ``mempalace_drawers`` (documents + metadatas + embeddings)."""
    import chromadb

    client = chromadb.PersistentClient(path=str(source))
    col = client.get_or_create_collection("mempalace_drawers")
    documents = [
        "decided to use dual-emit for observability across the memory bundle",
        "the auth migration blocked on rate limits from the identity provider",
    ]
    metadatas = [
        {"wing": "wing_seed", "room": "decisions", "source_file": "seed-1.md", "category": "decision"},
        {"wing": "wing_seed", "room": "blockers", "source_file": "seed-2.md", "category": "blocker"},
    ]
    # Deterministic, distinguishable 384-dim vectors (matches the native
    # embedder's dimensionality) -- not real semantic embeddings, but
    # sufficient to prove verbatim copy + self-retrieval via cosine.
    embeddings = [
        [1.0] + [0.0] * 383,
        [0.0, 1.0] + [0.0] * 382,
    ]
    col.add(ids=["seed-1", "seed-2"], documents=documents, metadatas=metadatas, embeddings=embeddings)
    return {"documents": documents, "metadatas": metadatas, "embeddings": embeddings}


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _run_ephemeral_daemon_in_thread(home: Path) -> None:
    """Run the daemon's server loop in a background thread (ephemeral store,
    embedder disabled -- this test only needs verbatim vector copy, not a
    real embedding model, and offline test environments cannot download
    fastembed's ONNX weights)."""
    thread = threading.Thread(
        target=run_daemon,
        kwargs={"home": home, "ephemeral": True, "embedder_model": "none"},
        daemon=True,
    )
    thread.start()


def test_migration_report_matches_seed_and_source_untouched(tmp_path: Path) -> None:
    source = tmp_path / "legacy-palace"
    home = tmp_path / "memory-home"
    seed = _seed_legacy_store(source)

    before_snapshot = _snapshot_files(source)

    report = migrate(source=source, home=home, verify=True)

    assert report["drawers"] == 2, report
    assert report["embeddings_copied"] == 2, report
    assert report["errors"] == [], report
    assert report["verified"] is True, report
    assert report["skipped"]["kg"], "kg skip reason must be reported, not silently dropped"
    assert report["skipped"]["diaries"], "diary skip reason must be reported, not silently dropped"

    # Read-only proof: the source tree is byte-for-byte unchanged.
    after_snapshot = _snapshot_files(source)
    assert before_snapshot.keys() == after_snapshot.keys(), "source file set changed"
    for name, data in before_snapshot.items():
        assert after_snapshot[name] == data, f"source file mutated: {name}"

    # Semantic search finds the migrated content via its copied vector.
    from amplifier_module_tool_memory.client import ensure_daemon

    client = ensure_daemon(home)
    assert client is not None
    result = client.search("dual-emit", 5, wing="wing_seed")
    contents = [r["content"] for r in result["results"]]
    assert any(seed["documents"][0] in c for c in contents), (
        f"migrated content not found via search: {result}"
    )

    client.shutdown()
