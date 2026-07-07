"""
amplifier-memory-import — one-shot, read-only migration from a legacy
ChromaDB-backed vendor store into the native amplifier-data memory store
(D7, \u00a79 of docs/plans/2026-07-07-native-cutover-design.md).

This module is the ONE permitted place (besides CHANGELOG.md and
docs/plans/*) where the legacy vendor's name may appear -- it exists
specifically to describe what it imports FROM.

Usage:
    amplifier-memory-import [--source PATH] [--home PATH] [--re-embed]
                             [--verify] [--wing NAME]

Requires the `[migrate]` extra (chromadb) -- NOT the legacy vendor package
itself, which is never imported or required by this script:

    pip install 'amplifier-module-tool-memory[migrate]'

Behavior (\u00a79 of the design doc):

1. **Preconditions.** ``--source`` defaults to ``~/.mempalace/palace``. Opened
   via ``chromadb.PersistentClient(path=source)`` -> collection
   ``mempalace_drawers`` (the verified on-disk layout of the legacy 3.5.0
   vendor release), READ-ONLY. The source tree is never modified.
2. **Drawers + embeddings.** Pages through
   ``collection.get(include=["documents","metadatas","embeddings"], ...)``.
   Per record: ``write_cell`` (content-addressed, idempotent by
   construction) + wing/room ``scope`` + ``has_source``/``has_category``/
   ``has_importance`` facts (each guarded read-before-write, \u00a79.4) + the
   stored embedding copied VERBATIM via the generic ``add_embedding`` tool
   (same MiniLM space as the native embedder, D1 -- no re-embed). A
   ``has_embedding_copied`` marker fact makes the embedding copy itself
   idempotent on re-run. ``--re-embed`` instead re-embeds the drawer through
   the daemon's own embedder (for users switching models).
3. **KG + diaries (best-effort, honest).** The legacy vendor persists KG
   triples and diaries outside the chroma collection in a format that is
   NOT independently verifiable from this repo without an installed copy of
   the vendor package. Per the design doc: "If the format is absent or
   unrecognized: report skipped loudly -- never guess a format." This
   implementation reports ``skipped: {kg: reason, diaries: reason}`` rather
   than fabricating an import.
4. **Idempotency + verify.** Content addressing dedups cells/scopes on
   re-run (same bytes -> same ref). Facts and the embedding copy each get a
   read-before-write guard. ``--verify`` re-reads every imported drawer
   through ``regenerate`` and byte-compares against the source document
   (this absorbs the retired ``dualwrite_compare`` role).
5. **Report (stdout JSON).**
   ``{drawers, embeddings_copied, kg_facts, diaries, skipped, errors, verified}``;
   exit 0 iff ``errors == 0``. Emits ``memory-import: import_completed``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .client import MemoryClient, ensure_daemon
from .daemon import default_memory_home
from .event_emitter import emit_event

#: Verified layout of the legacy vendor's 3.5.0 release (design doc \u00a79.1):
#: a Chroma collection literally named ``mempalace_drawers`` holding the
#: verbatim drawer documents, their filing metadata, and their embeddings.
_LEGACY_COLLECTION_NAME = "mempalace_drawers"

#: Default source path: the legacy vendor's on-disk store location.
_DEFAULT_SOURCE = Path.home() / ".mempalace" / "palace"

_PAGE_SIZE = 500


def _default_source() -> Path:
    return _DEFAULT_SOURCE


def _open_legacy_collection(source: Path) -> Any:
    """Open *source* read-only via chromadb and return the drawers collection.

    Raises ``RuntimeError`` with an actionable message if chromadb is not
    installed (the ``[migrate]`` extra) or the collection is missing.
    """
    try:
        import chromadb  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "amplifier-memory-import requires the '[migrate]' extra: "
            "pip install 'amplifier-module-tool-memory[migrate]'"
        ) from exc

    client = chromadb.PersistentClient(path=str(source))
    try:
        return client.get_collection(_LEGACY_COLLECTION_NAME)
    except Exception as exc:
        raise RuntimeError(
            f"collection '{_LEGACY_COLLECTION_NAME}' not found under {source} -- "
            "is this a legacy vendor palace directory?"
        ) from exc


def _iter_legacy_drawers(
    collection: Any,
) -> Iterator[tuple[str, str, dict[str, Any], list[float] | None]]:
    """Yield ``(doc_id, document, metadata, embedding)`` tuples, paginated.

    Read-only: only ``collection.get(...)`` is ever called, never a write
    method. Pages of ``_PAGE_SIZE`` at a time so large stores don't require
    loading the whole collection into memory at once.
    """
    offset = 0
    while True:
        page = collection.get(
            include=["documents", "metadatas", "embeddings"],
            limit=_PAGE_SIZE,
            offset=offset,
        )
        ids = page.get("ids") or []
        if not ids:
            return
        documents = page.get("documents") or []
        metadatas = page.get("metadatas") or []
        embeddings = page.get("embeddings")
        if embeddings is None:
            embeddings = [None] * len(ids)
        for doc_id, document, metadata, embedding in zip(
            ids, documents, metadatas, embeddings, strict=False
        ):
            yield doc_id, document, metadata or {}, embedding
        offset += len(ids)
        if len(ids) < _PAGE_SIZE:
            return


def _fact_present(client: MemoryClient, ref: str, predicate: str, value: str) -> bool:
    """Read-before-write guard (\u00a79.4): is *predicate* already asserted on *ref*
    with a matching value? Best-effort -- any read failure is treated as
    "not present" so import proceeds (never blocks on a flaky read)."""
    try:
        result = client.query_facts(subject=ref, predicate=predicate)
        if not result.success:
            return False
        for fact in result.output:
            cell = client.regenerate(fact.object)
            if cell.payload.decode("utf-8", errors="replace") == value:
                return True
        return False
    except Exception:
        return False


def _assert_fact_if_missing(
    client: MemoryClient, ref: str, predicate: str, value: str
) -> bool:
    """Assert ``(ref, predicate, value)`` unless already present. Returns True
    if a new fact was written."""
    if _fact_present(client, ref, predicate, value):
        return False
    client.assert_fact(ref, predicate, client.write_cell(value.encode("utf-8")))
    return True


def _import_one_drawer(
    client: MemoryClient,
    *,
    text: str,
    wing: str,
    room: str,
    source_file: str,
    category: str | None,
    importance: float | None,
    embedding: list[float] | None,
    re_embed: bool,
) -> tuple[str, bool]:
    """Import one legacy drawer. Returns ``(ref, embedding_copied)``.

    Every step is individually idempotent: ``write_cell``/``scope`` via
    content addressing, facts via :func:`_assert_fact_if_missing`, and the
    embedding copy via a ``has_embedding_copied`` marker fact.
    """
    ref = client.write_cell(text.encode("utf-8"))
    client.scope(ref, client.write_cell(f"wing:{wing}".encode()))
    client.scope(ref, client.write_cell(f"room:{room}".encode()))

    if source_file:
        _assert_fact_if_missing(client, ref, "has_source", source_file)
    if category is not None:
        _assert_fact_if_missing(client, ref, "has_category", str(category))
    if importance is not None:
        _assert_fact_if_missing(client, ref, "has_importance", str(importance))

    embedding_copied = False
    if re_embed:
        # Re-embed path: `remember` re-files through the daemon's current
        # embedder. The drawer cell/scopes/facts above are already correct
        # (content-addressed to the same ref); `remember` will resolve to
        # the same cell and simply attach a fresh embedding.
        try:
            client.remember(
                wing=wing,
                room=room,
                content=text,
                source=source_file,
                category=category,
                importance=importance,
            )
            embedding_copied = True
        except Exception:
            embedding_copied = False
    elif embedding is not None and not _fact_present(
        client, ref, "has_embedding_copied", "true"
    ):
        client.add_embedding(ref, [float(x) for x in embedding])
        client.assert_fact(ref, "has_embedding_copied", client.write_cell(b"true"))
        embedding_copied = True

    return ref, embedding_copied


def _detect_legacy_kg_and_diaries(source: Path) -> tuple[str, str]:
    """Best-effort detection of a legacy KG/diary artifact next to *source*.

    Returns ``(kg_skip_reason, diary_skip_reason)``. No verified on-disk
    format for either exists in this repo without an installed copy of the
    legacy vendor package (design doc \u00a79.3) -- honest skip, never a guess.
    """
    kg_reason = (
        "no verified on-disk KG format for the legacy vendor store is available "
        "in this environment (would require inspecting an installed copy of "
        "the legacy package's source to confirm the schema) -- skipped rather "
        "than guessed"
    )
    diary_reason = (
        "no verified on-disk diary format for the legacy vendor store is "
        "available in this environment -- skipped rather than guessed"
    )
    return kg_reason, diary_reason


def migrate(
    *,
    source: Path,
    home: Path | None = None,
    re_embed: bool = False,
    verify: bool = False,
    default_wing: str = "wing_general",
) -> dict[str, Any]:
    """Run one migration pass. Returns the \u00a79.5 report dict. Never raises for
    per-record errors (accumulated in ``errors``); raises only if the source
    collection cannot be opened at all or the daemon is unavailable."""
    collection = _open_legacy_collection(source)

    client = ensure_daemon(home)
    if client is None:
        raise RuntimeError("memory daemon unavailable -- cannot import")

    drawers = 0
    embeddings_copied = 0
    errors: list[str] = []
    imported: list[tuple[str, str]] = []  # (ref, original_content)

    for doc_id, document, metadata, embedding in _iter_legacy_drawers(collection):
        try:
            text = str(document or "")
            wing = str(metadata.get("wing") or default_wing)
            room = str(metadata.get("room") or "imported")
            source_file = str(
                metadata.get("source_file") or metadata.get("source") or ""
            )
            category = metadata.get("category")
            importance_raw = metadata.get("importance")
            importance = float(importance_raw) if importance_raw is not None else None
            vector = None if embedding is None else [float(x) for x in embedding]

            ref, embedding_copied = _import_one_drawer(
                client,
                text=text,
                wing=wing,
                room=room,
                source_file=source_file,
                category=category,
                importance=importance,
                embedding=vector,
                re_embed=re_embed,
            )
            drawers += 1
            if embedding_copied:
                embeddings_copied += 1
            imported.append((ref, text))
        except Exception as exc:
            errors.append(f"{doc_id}: {exc}")

    kg_skip_reason, diary_skip_reason = _detect_legacy_kg_and_diaries(source)
    skipped = {"kg": kg_skip_reason, "diaries": diary_skip_reason}

    verified: bool | None = None
    if verify:
        verified = True
        for ref, original in imported:
            try:
                cell = client.regenerate(ref)
                if cell.payload != original.encode("utf-8"):
                    verified = False
                    errors.append(f"{ref}: verify byte-mismatch")
            except Exception as exc:
                verified = False
                errors.append(f"{ref}: verify failed: {exc}")

    report = {
        "drawers": drawers,
        "embeddings_copied": embeddings_copied,
        "kg_facts": 0,
        "diaries": 0,
        "skipped": skipped,
        "errors": errors,
        "verified": verified,
    }

    try:
        emit_event(
            "memory-import",
            "import_completed",
            ok=not errors,
            data={
                "drawers": drawers,
                "embeddings_copied": embeddings_copied,
                "errors": len(errors),
                "verified": verified,
            },
        )
    except Exception:
        pass

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amplifier-memory-import")
    parser.add_argument(
        "--source",
        default=None,
        help="legacy vendor palace directory (default: ~/.mempalace/palace)",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="target memory home (default: ~/.amplifier/memory or $AMPLIFIER_MEMORY_HOME)",
    )
    parser.add_argument(
        "--re-embed",
        action="store_true",
        help="re-embed through the daemon's current model instead of copying vectors verbatim",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-read every imported drawer and byte-compare against the source",
    )
    parser.add_argument(
        "--wing",
        default="wing_general",
        help="fallback wing for drawers whose metadata carries no wing (default: wing_general)",
    )
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser() if args.source else _default_source()
    home = Path(args.home).expanduser() if args.home else default_memory_home()

    try:
        report = migrate(
            source=source,
            home=home,
            re_embed=args.re_embed,
            verify=args.verify,
            default_wing=args.wing,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"amplifier-memory-import: {exc}\n")
        return 1

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
