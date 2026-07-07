"""
KG-R1: the extended dualwrite_compare harness proves the substrate can answer
memory's read shapes (vector, KG, scope, diary) consistently with the palace,
in addition to the existing E1/scope/facts/durability checks.

Invokes run_compare() in-process on the representative corpus and asserts the
new report fields are present and green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_mempalace.scripts.dualwrite_compare import (  # noqa: E402
    representative_samples,
    run_compare,
)


def test_run_compare_new_fields_all_green(tmp_path: Path) -> None:
    cells = representative_samples()
    report = run_compare(cells, tmp_path / "shadow.ampd", "representative corpus (test)")

    # Vector: every embedded cell self-retrieves top-1 within its scope.
    assert "vector_top1_ok" in report
    assert "vector_scoped_total" in report
    assert report["vector_top1_ok"] == report["vector_scoped_total"]

    # KG: assert/invalidate/timeline validity window checks.
    assert report["kg_assert_ok"] is True
    assert report["kg_invalidate_ok"] is True
    assert report["kg_timeline_ok"] is True

    # Scope query consistency.
    assert report["scope_query_consistent"] is True

    # Diary round-trip.
    assert report["diary_ok"] is True


def test_run_compare_with_explicit_embeddings(tmp_path: Path) -> None:
    """Cells carrying a real ``embedding`` list are transported and self-retrieve."""
    cells = [
        {
            "wing": "wing_emb",
            "room": "r",
            "content": "cell with a real embedding",
            "source": "test",
            "category": "decision",
            "importance": 0.6,
            "embedding": [1.0, 0.0, 0.0, 0.0],
        },
        {
            "wing": "wing_emb",
            "room": "r",
            "content": "another cell, different vector",
            "source": "test",
            "category": None,
            "importance": None,
            "embedding": [0.0, 1.0, 0.0, 0.0],
        },
    ]
    report = run_compare(cells, tmp_path / "shadow2.ampd", "explicit-embeddings (test)")
    assert report["vector_top1_ok"] == report["vector_scoped_total"] == 2
    assert report.get("embedding_source") in {"real", "mixed"}


def test_cli_main_exits_zero_on_representative_corpus(tmp_path: Path, monkeypatch) -> None:
    """KG-R1: mempalace-dualwrite-compare exits 0 with all new fields green."""
    from amplifier_module_tool_mempalace.scripts import dualwrite_compare as mod

    # Force the representative-corpus fallback path (no real events dir).
    empty_events = tmp_path / "no_events"
    argv = ["--events-dir", str(empty_events), "--limit", "50"]
    rc = mod.main(argv)
    assert rc == 0
