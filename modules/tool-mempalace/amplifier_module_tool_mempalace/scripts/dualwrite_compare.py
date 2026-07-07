"""
dualwrite_compare — §8 migration harness for the amplifier-data substrate.

Loads a sample of REAL captured content from the MemPalace event log, dual-writes
it (palace-mirror as source of truth + amplifier-data as the shadow), then runs
the regeneration-equivalence compare:

  * E1: every shadowed cell regenerates byte-for-byte equal to the source content.
  * scope: wing/room land as `scoped_to` edges.
  * facts: category/importance land as queryable KG facts.
  * durability: close + reopen the durable store; cells still regenerate identically.

The palace stays source of truth — this never writes to the real palace; it reads
the event log read-only and writes amplifier-data into a throwaway path.

Run:
    mempalace-dualwrite-compare [--events-dir DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from amplifier_module_tool_mempalace.scripts.memory_store import (
    AmplifierDataMemoryStore,
    DualWriteMemoryStore,
    RecordingMemoryStore,
)
from amplifier_module_tool_mempalace.scripts.write_cells import write_cells

_PREVIEW_EVENTS = {"drawer_filed", "capture_queued", "capture_skipped"}

_SYNTHETIC_VECTOR_DIM = 8


def _synthetic_vector(content: str, dim: int = _SYNTHETIC_VECTOR_DIM) -> list[float]:
    """Deterministic unit-ish vector seeded from content sha256.

    Proves the routing/scoping/regeneration machinery even with no real
    embedding available. Same content always yields the same vector
    (idempotent, matching content-addressing semantics elsewhere in the seam).
    """
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    return [(digest[i % len(digest)] / 255.0) * 2 - 1 for i in range(dim)]


def load_real_samples(events_dir: Path, limit: int) -> list[dict[str, Any]]:
    """Extract real captured-content fragments (event previews) as cells.

    The palace on this machine has no filed drawers, so the realest available
    content is the `preview` field on capture events — genuine tool-output
    fragments the capture hook actually saw. Deduplicated by content.
    """
    cells: list[dict[str, Any]] = []
    seen: set[str] = set()
    files = sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []
    for fpath in files:
        if len(cells) >= limit:
            break
        try:
            lines = fpath.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if len(cells) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("event") not in _PREVIEW_EVENTS:
                continue
            preview = ev.get("preview")
            if not preview or not isinstance(preview, str) or len(preview) < 20:
                continue
            if preview in seen:
                continue
            seen.add(preview)
            data = ev.get("data") or {}
            cells.append(
                {
                    "wing": "wing_migration_sample",
                    "room": str(ev.get("hook") or data.get("tool_name") or "events"),
                    "content": preview,
                    "source": str(data.get("source") or ev.get("hook") or ""),
                    "category": data.get("category"),
                    "importance": None,
                }
            )
    return cells


def representative_samples() -> list[dict[str, Any]]:
    """Drawer-shaped fallback corpus used when no real captures are available.

    Clearly labelled as representative in the report — NOT extracted real drawers.
    """
    raw = [
        (
            "wing_demo",
            "auth-migration",
            "We decided to externalize the capture taxonomy into a user-editable manifest.",
            "decision",
            0.75,
        ),
        (
            "wing_demo",
            "auth-migration",
            "The build failed with a circular import between the hook and the loader.",
            "blocker",
            0.65,
        ),
        (
            "wing_demo",
            "api-design",
            "load_manifest resolution order: explicit -> project -> home -> in-code default.",
            "architecture",
            0.70,
        ),
        (
            "wing_demo",
            "api-design",
            "Fixed: guarded the import so a missing tool-mempalace degrades to legacy signals.",
            "resolved_blocker",
            0.55,
        ),
        (
            "wing_other",
            "perf",
            "Turns out brute-force cosine over 40k drawers per interject is the latency risk.",
            "lesson_learned",
            0.45,
        ),
        (
            "wing_other",
            "perf",
            "Convention: always pass scope= on lens reads; never global top-k for scoped search.",
            "pattern",
            0.50,
        ),
        (
            "wing_other",
            "deps",
            "amplifier-data requires you to bring your own embedder; ChromaDB embeds internally.",
            "dependency",
            0.50,
        ),
        (
            "wing_demo",
            "notes",
            "Plain note with unicode 世界 and a trailing newline.\n",
            None,
            None,
        ),
    ]
    return [
        {
            "wing": w,
            "room": r,
            "content": c,
            "source": "representative",
            "category": cat,
            "importance": imp,
        }
        for (w, r, c, cat, imp) in raw
    ]


def run_compare(
    cells: list[dict[str, Any]], store_path: Path, content_source: str = "unknown"
) -> dict[str, Any]:
    """Dual-write and run the equivalence compare. Returns a structured report.

    Beyond the original E1/scope/facts/durability loop, this also proves the
    substrate can ANSWER memory's read shapes (vector search, KG facts/graph,
    scoped reads, diary) consistently with the palace-mirror — verification
    only; the palace remains the only production read source (§4e of
    docs/plans/2026-07-07-substrate-adapter-completion-design.md).
    """
    palace = RecordingMemoryStore()  # stand-in for the source-of-truth palace

    # Attach a vector to every cell missing one: real (caller-supplied, e.g.
    # exported from ChromaDB) wins; otherwise a deterministic synthetic
    # unit-ish vector seeded from content. Either way vector routing is
    # exercised for the whole corpus, not just cells that happen to carry a
    # real embedding.
    had_real_embedding = any(c.get("embedding") is not None for c in cells)
    prepared_cells: list[dict[str, Any]] = []
    for cell in cells:
        prepared = dict(cell)
        if prepared.get("embedding") is None:
            prepared["embedding"] = _synthetic_vector(str(prepared["content"]))
        prepared_cells.append(prepared)
    embedding_source = (
        "real"
        if had_real_embedding and all(c.get("embedding") is not None for c in cells)
        else ("mixed" if had_real_embedding else "synthetic")
    )

    shadow = AmplifierDataMemoryStore(path=str(store_path), record_access=False)
    dual = DualWriteMemoryStore(palace, shadow)

    written = write_cells(prepared_cells, dual)

    s = shadow.store
    e1_ok = 0
    e1_bad: list[str] = []
    scope_ok = 0
    facts_ok = 0
    vector_top1_ok = 0
    vector_scoped_total = 0
    wing_expected: dict[str, int] = defaultdict(int)
    wing_actual: dict[str, int] = defaultdict(int)
    for rec in shadow.filed:
        ref = rec["ref"]
        original = str(rec["content"]).encode("utf-8")
        wing_expected[str(rec["wing"])] += 1
        if s.regenerate(ref).payload == original:
            e1_ok += 1
        else:
            e1_bad.append(str(ref)[:12])
        wing_ref = s.write_cell(f"wing:{rec['wing']}".encode())
        neighbors = s.graph_neighbors(ref, rel_type="scoped_to")
        labels = {s.regenerate(n).payload.decode("utf-8", "replace") for n in neighbors}
        if f"wing:{rec['wing']}" in labels and f"room:{rec['room']}" in labels:
            scope_ok += 1
        if wing_ref in neighbors:
            wing_actual[str(rec["wing"])] += 1
        if rec["category"] is not None:
            got = s.query_facts(subject=ref, predicate="has_category")
            if got.success and got.output:
                facts_ok += 1
        # Vector self-retrieval: querying a cell's OWN vector, scoped to its
        # wing, must return that cell top-1 (exact, deterministic).
        if rec.get("embedding") is not None:
            vector_scoped_total += 1
            hits = s.query_vector(rec["embedding"], 1, scope=wing_ref)
            if hits and hits[0][0] == ref:
                vector_top1_ok += 1

    scope_query_consistent = dict(wing_actual) == dict(wing_expected)

    # KG: assert a fact per categorized cell (drawer:<n>, has_category, <cat>)
    # through the seam's anchor-cell KG surface, verify query_kg sees it,
    # invalidate one, verify the validity window in kg_timeline.
    categorized_indices = [i for i, r in enumerate(shadow.filed) if r["category"] is not None]
    kg_assert_ok = True
    for i in categorized_indices:
        rec = shadow.filed[i]
        subject = f"drawer:{i}"
        shadow.assert_kg(subject, "has_category", str(rec["category"]))
        if (subject, "has_category", str(rec["category"])) not in shadow.query_kg(
            subject=subject
        ):
            kg_assert_ok = False

    kg_invalidate_ok = True
    kg_timeline_ok = True
    if categorized_indices:
        i0 = categorized_indices[0]
        rec0 = shadow.filed[i0]
        subject0 = f"drawer:{i0}"
        cat0 = str(rec0["category"])
        shadow.invalidate_kg(subject0, "has_category", cat0)
        kg_invalidate_ok = (subject0, "has_category", cat0) not in shadow.query_kg(
            subject=subject0
        )
        timeline = shadow.kg_timeline(subject0)
        ops = [t["op"] for t in timeline if t["predicate"] == "has_category"]
        kg_timeline_ok = ops == ["assert", "invalidate"]

    # Diary: file one entry, read it back via the agent: scope, byte-compare.
    diary_agent = "dualwrite-harness"
    diary_entry = "dualwrite compare diary round-trip check"
    diary_ref = shadow.file_diary(agent_name=diary_agent, entry=diary_entry, topic="compare")
    agent_scope_ref = s.write_cell(f"agent:{diary_agent}".encode())
    diary_ok = (
        s.regenerate(diary_ref).payload == diary_entry.encode("utf-8")
        and agent_scope_ref in s.graph_neighbors(diary_ref, rel_type="scoped_to")
    )

    # durability: reopen the store from disk, re-check E1 + vector on every cell
    s.close()
    reopened = AmplifierDataMemoryStore(path=str(store_path), record_access=False)
    durable_ok = 0
    durable_vector_ok = 0
    for rec in shadow.filed:
        if reopened.store.regenerate(rec["ref"]).payload == str(rec["content"]).encode(
            "utf-8"
        ):
            durable_ok += 1
        if rec.get("embedding") is not None:
            wing_ref = reopened.store.write_cell(f"wing:{rec['wing']}".encode())
            hits = reopened.store.query_vector(rec["embedding"], 1, scope=wing_ref)
            if hits and hits[0][0] == rec["ref"]:
                durable_vector_ok += 1
    reopened.store.close()

    categorized = sum(1 for r in shadow.filed if r["category"] is not None)
    return {
        "content_source": content_source,
        "embedding_source": embedding_source,
        "samples": len(cells),
        "written": written,
        "palace_mirror": len(palace.filed),
        "shadow_filed": len(shadow.filed),
        "shadow_errors": dual.shadow_errors,
        "e1_byte_identical": e1_ok,
        "e1_mismatches": e1_bad,
        "scope_edges_ok": scope_ok,
        "categorized": categorized,
        "facts_ok": facts_ok,
        "durable_reopen_ok": durable_ok,
        "durable_vector_ok": durable_vector_ok,
        "vector_top1_ok": vector_top1_ok,
        "vector_scoped_total": vector_scoped_total,
        "scope_query_consistent": scope_query_consistent,
        "kg_assert_ok": kg_assert_ok,
        "kg_invalidate_ok": kg_invalidate_ok,
        "kg_timeline_ok": kg_timeline_ok,
        "diary_ok": diary_ok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mempalace-dualwrite-compare")
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".mempalace" / "events"),
        help="MemPalace event-log directory to sample real content from.",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--content-file",
        default=None,
        help="JSON list of real drawer dicts {wing,room,content,category} to compare.",
    )
    parser.add_argument(
        "--embeddings-file",
        default=None,
        help=(
            "Optional JSON {content_sha256: [floats]} to attach real palace "
            "vectors (e.g. exported from ChromaDB) to --content-file corpora. "
            "Cells with no matching entry fall back to a synthetic vector."
        ),
    )
    args = parser.parse_args(argv)

    if args.content_file:
        loaded = json.loads(Path(args.content_file).read_text(encoding="utf-8"))
        cells = [
            {
                "wing": c.get("wing", "wing_real"),
                "room": c.get("room", "real"),
                "content": c["content"],
                "source": c.get("source", ""),
                "category": c.get("category"),
                "importance": c.get("importance"),
                "embedding": c.get("embedding"),
            }
            for c in loaded[: args.limit]
            if isinstance(c, dict) and c.get("content")
        ]
        content_source = f"real palace drawers ({args.content_file})"
    elif cells := load_real_samples(Path(args.events_dir), args.limit):
        content_source = f"real event-log previews ({args.events_dir})"
    else:
        # The local palace had no filed drawers / previews were disabled.
        # Fall back to a labelled representative corpus — the equivalence
        # invariants are content-agnostic; "real" only adds credibility.
        cells = representative_samples()
        content_source = "representative corpus (local palace empty — no real drawers)"

    if args.embeddings_file:
        real_vectors = json.loads(Path(args.embeddings_file).read_text(encoding="utf-8"))
        for cell in cells:
            if cell.get("embedding") is not None:
                continue
            sha = hashlib.sha256(str(cell["content"]).encode("utf-8")).hexdigest()
            if sha in real_vectors:
                cell["embedding"] = real_vectors[sha]

    with tempfile.TemporaryDirectory() as td:
        report = run_compare(cells, Path(td) / "shadow.ampd", content_source)

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    ok = (
        report["e1_byte_identical"] == report["shadow_filed"]
        and not report["e1_mismatches"]
        and report["scope_edges_ok"] == report["shadow_filed"]
        and report["durable_reopen_ok"] == report["shadow_filed"]
        and report["facts_ok"] == report["categorized"]
        and report["vector_top1_ok"] == report["vector_scoped_total"]
        and report["durable_vector_ok"] == report["vector_scoped_total"]
        and report["kg_assert_ok"]
        and report["kg_invalidate_ok"]
        and report["kg_timeline_ok"]
        and report["scope_query_consistent"]
        and report["diary_ok"]
    )
    sys.stdout.write(("PASS\n" if ok else "FAIL\n"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
