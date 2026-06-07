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
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from amplifier_module_tool_mempalace.scripts.memory_store import (
    AmplifierDataMemoryStore,
    DualWriteMemoryStore,
    RecordingMemoryStore,
)
from amplifier_module_tool_mempalace.scripts.write_cells import write_cells

_PREVIEW_EVENTS = {"drawer_filed", "capture_queued", "capture_skipped"}


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
    """Dual-write and run the equivalence compare. Returns a structured report."""
    palace = RecordingMemoryStore()  # stand-in for the source-of-truth palace
    shadow = AmplifierDataMemoryStore(path=str(store_path), record_access=False)
    dual = DualWriteMemoryStore(palace, shadow)

    written = write_cells(cells, dual)

    s = shadow.store
    e1_ok = 0
    e1_bad: list[str] = []
    scope_ok = 0
    facts_ok = 0
    for rec in shadow.filed:
        ref = rec["ref"]
        original = str(rec["content"]).encode("utf-8")
        if s.regenerate(ref).payload == original:
            e1_ok += 1
        else:
            e1_bad.append(str(ref)[:12])
        labels = {
            s.regenerate(n).payload.decode("utf-8", "replace")
            for n in s.graph_neighbors(ref, rel_type="scoped_to")
        }
        if f"wing:{rec['wing']}" in labels and f"room:{rec['room']}" in labels:
            scope_ok += 1
        if rec["category"] is not None:
            got = s.query_facts(subject=ref, predicate="has_category")
            if got.success and got.output:
                facts_ok += 1

    # durability: reopen the store from disk, re-check E1 on every cell
    s.close()
    reopened = AmplifierDataMemoryStore(path=str(store_path), record_access=False)
    durable_ok = sum(
        1
        for rec in shadow.filed
        if reopened.store.regenerate(rec["ref"]).payload
        == str(rec["content"]).encode("utf-8")
    )
    reopened.store.close()

    categorized = sum(1 for r in shadow.filed if r["category"] is not None)
    return {
        "content_source": content_source,
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mempalace-dualwrite-compare")
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".mempalace" / "events"),
        help="MemPalace event-log directory to sample real content from.",
    )
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    cells = load_real_samples(Path(args.events_dir), args.limit)
    if cells:
        content_source = f"real event-log previews ({args.events_dir})"
    else:
        # The local palace had no filed drawers / previews were disabled.
        # Fall back to a labelled representative corpus — the equivalence
        # invariants are content-agnostic; "real" only adds credibility.
        cells = representative_samples()
        content_source = "representative corpus (local palace empty — no real drawers)"

    with tempfile.TemporaryDirectory() as td:
        report = run_compare(cells, Path(td) / "shadow.ampd", content_source)

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    ok = (
        report["e1_byte_identical"] == report["shadow_filed"]
        and not report["e1_mismatches"]
        and report["scope_edges_ok"] == report["shadow_filed"]
        and report["durable_reopen_ok"] == report["shadow_filed"]
        and report["facts_ok"] == report["categorized"]
    )
    sys.stdout.write(("PASS\n" if ok else "FAIL\n"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
