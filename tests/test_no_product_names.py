"""
Product-name-zero grep gate.

Encodes the "no remnants of the original products" sweep as an executable
test so it runs in every CI pass rather than relying on a human to remember
to run it manually. Two prior generations of naming are covered by one
pattern: the legacy vendor this bundle was originally built on (mempalace /
"palace" branding, ChromaDB backend) and the ten surveyed prior-art memory
projects (Mem0, OpenMemory, Hindsight, Vectorize, memU/NevaMind,
Cognee/Topoteretes, Graphiti/Zep/getzep, OpenViking/Volcengine, Letta/MemGPT,
enola).

The full audit trail for both generations -- every literal legacy name, the
complete pre-scrub rename tables, and the gene-survey evidence record -- now
lives outside this repo (external research record:
memthoughts/research/amplifier-memory-v2.1/, not part of this bundle).
project-context/PROVENANCE.md, CHANGELOG.md, and
project-context/EXPERIMENT_JOURNAL.md have been rewritten to neutral
technical language and carry zero hits themselves; they are NOT allowlisted
here.

Allowlist: only benchmarks/amb/ (see below) and this file itself.

Plus benchmarks/amb/: the external benchmark harness this bundle plugs into
is named "Agent Memory Benchmark" (AMB) and lives at
github.com/vectorize-io/agent-memory-benchmark. That name and git URL are
allowed ONLY inside benchmarks/amb/, and only the literal tokens
"agent-memory-benchmark" and "vectorize-io" -- any other product mention in
that directory still fails the gate.

Plus this file itself, which must name every pattern to define it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".mypy_cache",
}

#: Full-file exemptions: only the gate file itself. benchmarks/amb/ is
#: handled separately below via token-stripping, not a full-file exemption.
_ALLOWLISTED_FILES = {
    # This file: describes/defines the pattern itself.
    REPO_ROOT / "tests" / "test_no_product_names.py",
}

_TEXT_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".md", ".cfg", ".ini", ".dot")

#: The full pattern: legacy vendor branding + the ten surveyed prior-art
#: memory projects. Short/ambiguous tokens are word-bounded.
_PRODUCT_RE = re.compile(
    r"mem0|openmemory|hindsight|vectorize|\bmemu\b|nevamind|cognee|"
    r"topoteretes|graphiti|getzep|\bzep\b|openviking|volcengine|\bviking\b|"
    r"letta|memgpt|mempalace|palace|chromadb|chroma|enola",
    re.IGNORECASE,
)

#: The AMB external benchmark tool's name/URL tokens, allowed ONLY inside
#: benchmarks/amb/.
_AMB_EXEMPT_TOKENS = ("agent-memory-benchmark", "vectorize-io")
_AMB_DIR = REPO_ROOT / "benchmarks" / "amb"


def _iter_repo_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix == ".lock":
            continue
        yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _strip_amb_exempt_tokens(line: str) -> str:
    """Remove the AMB name/URL tokens from a line before pattern-matching it.

    Only applied to files under benchmarks/amb/ -- everywhere else these
    tokens are not exempt (e.g. "vectorize" alone still fails the gate).
    """
    for token in _AMB_EXEMPT_TOKENS:
        line = re.sub(re.escape(token), "", line, flags=re.IGNORECASE)
    return line


def test_no_product_names_outside_allowlist() -> None:
    """No prior-generation vendor/product name may appear anywhere in the
    repo except the allowlist above."""
    violations: list[tuple[str, int, str]] = []
    for path in _iter_repo_files():
        if path in _ALLOWLISTED_FILES:
            continue
        text = _read_text(path)
        if not text:
            continue
        in_amb = (
            path.is_relative_to(_AMB_DIR)
            if hasattr(path, "is_relative_to")
            else str(path).startswith(str(_AMB_DIR))
        )
        for i, line in enumerate(text.splitlines(), start=1):
            check_line = _strip_amb_exempt_tokens(line) if in_amb else line
            if _PRODUCT_RE.search(check_line):
                violations.append(
                    (str(path.relative_to(REPO_ROOT)), i, line.strip()[:120])
                )

    assert not violations, (
        "product-name gate FAIL: prior-generation product name found "
        "outside the allowlist:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in violations)
    )


def test_no_product_name_in_file_paths() -> None:
    """No file path in the repo may contain 'palace' or 'mempalace' -- the
    rename away from that branding must be complete, not just in content."""
    violations: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if re.search(r"palace|mempalace", rel, re.IGNORECASE):
            violations.append(rel)

    assert not violations, (
        "product-name gate FAIL: file path contains legacy branding:\n"
        + "\n".join(f"  {p}" for p in violations)
    )
