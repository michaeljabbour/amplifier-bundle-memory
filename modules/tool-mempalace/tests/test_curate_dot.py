"""
Validate the cold-path consolidation pipeline artifact, pipelines/curate.dot.

Two layers:
  * Structural checks (always run): the DOT file exists and declares the
    expected node shapes, the convergence goal_gate, and the entry-point
    tool_commands the pipeline shells out to.
  * Engine validation (best-effort): if the amplifier-bundle-attractor
    loop-pipeline engine is importable, actually parse + validate the graph
    against the real grammar. Skipped when attractor is not available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CURATE_DOT = _REPO_ROOT / "pipelines" / "curate.dot"


def test_curate_dot_exists() -> None:
    assert _CURATE_DOT.is_file(), f"missing pipeline artifact: {_CURATE_DOT}"


def test_curate_dot_structure() -> None:
    src = _CURATE_DOT.read_text(encoding="utf-8")
    # node shapes that make this a valid attractor pipeline
    assert "shape=Mdiamond" in src  # start
    assert "shape=Msquare" in src  # exit
    assert "shape=parallelogram" in src  # tool nodes
    assert "shape=box" in src  # LLM nodes
    # the convergence ("attractor") cycle
    assert "goal_gate=true" in src
    assert "retry_target=classify" in src
    # the entry-point tool commands (must match pyproject [project.scripts])
    assert "mempalace-load-captures" in src
    assert "mempalace-write-cells" in src


def _load_attractor_parser():
    """Best-effort import of the attractor parser; return (parse_dot, validate) or None."""
    candidates = [
        None,  # already installed / on path
        str(
            Path.home()
            / "dev"
            / "amplifier-bundle-attractor"
            / "modules"
            / "loop-pipeline"
        ),
    ]
    for path in candidates:
        if path is not None and path not in sys.path:
            sys.path.insert(0, path)
        try:
            from amplifier_module_loop_pipeline.dot_parser import parse_dot
            from amplifier_module_loop_pipeline.validation import validate_or_raise

            return parse_dot, validate_or_raise
        except Exception:
            continue
    return None


def test_curate_dot_validates_against_attractor_engine() -> None:
    loaded = _load_attractor_parser()
    if loaded is None:
        pytest.skip("amplifier-bundle-attractor loop-pipeline engine not available")
    parse_dot, validate_or_raise = loaded
    graph = parse_dot(_CURATE_DOT.read_text(encoding="utf-8"))
    # parses into the expected number of nodes
    nodes = getattr(graph, "nodes", {})
    assert len(nodes) == 7
    # passes the engine's own structural validation (raises on failure)
    validate_or_raise(graph)
