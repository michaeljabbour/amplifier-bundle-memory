"""
KG-N4: vendor-zero grep gate (docs/plans/2026-07-07-native-cutover-design.md
\u00a712).

Encodes the grep gate as an executable test so it runs in every CI pass
rather than relying on a human to remember to run it manually.

Allowlist (per the design doc's \u00a712 KG-N4 definition): the migration module
(migrate.py, which must describe what it imports FROM), and the historical
dirs explicitly carved out of the B3 sweep (docs/plans/, project-context/,
CHANGELOG.md). Additionally, a small number of test/profile files that must
name the legacy vendor package literally in order to assert its ABSENCE
(you cannot test for the absence of a package without naming it) are
allowlisted here explicitly and enumerated -- any new "mempalace" string
outside this exact allowlist fails the gate.
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
_EXCLUDE_DIR_PREFIXES = (
    REPO_ROOT / "docs" / "plans",
    REPO_ROOT / "project-context",
)
_EXCLUDE_FILES = {
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "modules/tool-memory/amplifier_module_tool_memory/migrate.py",
}

#: Files that legitimately must name the legacy vendor package literally --
#: either to assert its ABSENCE (you cannot test for the absence of a
#: package without naming it) or to seed/read a throwaway legacy-shaped
#: store for the migration DTU profile. Each entry is reviewed and
#: intentional; this is not a general escape hatch.
_ALLOWLISTED_MEMPALACE_FILES = {
    REPO_ROOT / "modules/tool-memory/tests/conftest.py",
    REPO_ROOT / "modules/hooks-memory-briefing/tests/conftest.py",
    REPO_ROOT / "modules/hooks-memory-capture/tests/conftest.py",
    REPO_ROOT / "modules/hooks-memory-interject/tests/conftest.py",
    REPO_ROOT / "tests/integration/test_native_smoke.py",
    # KG-N5 migration test: must seed a legacy-shaped chromadb collection
    # (collection name "mempalace_drawers") to prove the importer works.
    REPO_ROOT / "modules/tool-memory/tests/test_migrate_kg_n5.py",
    REPO_ROOT / ".amplifier/digital-twin-universe/profiles/memory-migration-e2e.yaml",
    REPO_ROOT / ".amplifier/digital-twin-universe/profiles/memory-native-e2e.yaml",
    REPO_ROOT
    / "tests"
    / "test_vendor_sweep.py",  # this file: describes the allowlist itself
}

_TEXT_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".md", ".lock", ".cfg", ".ini")

_MEMPALACE_RE = re.compile(r"mempalace|\.mempalace", re.IGNORECASE)
_PALACE_RE = re.compile(r"palace", re.IGNORECASE)

#: \u00a712 KG-N4's second grep is scoped to these directories/files only.
_PALACE_SCOPE_DIRS = ("modules", "behaviors", "skills", "context", "agents")
_PALACE_SCOPE_FILES = ("bundle.md", "README.md")


def _iter_repo_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _EXCLUDE_DIRS for part in path.parts):
            continue
        if any(
            str(path).startswith(str(prefix) + "/") or path == prefix
            for prefix in _EXCLUDE_DIR_PREFIXES
        ):
            continue
        if path in _EXCLUDE_FILES:
            continue
        yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_no_mempalace_strings_outside_allowlist() -> None:
    """First grep: 'mempalace' / '.mempalace' must not appear anywhere in the
    repo except the migration module, CHANGELOG.md, docs/plans/,
    project-context/, and the small explicit allowlist above."""
    violations: list[tuple[str, int, str]] = []
    for path in _iter_repo_files():
        if path in _ALLOWLISTED_MEMPALACE_FILES:
            continue
        text = _read_text(path)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if _MEMPALACE_RE.search(line):
                violations.append(
                    (str(path.relative_to(REPO_ROOT)), i, line.strip()[:120])
                )

    assert not violations, (
        "KG-N4 FAIL: 'mempalace' found outside the allowlist:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in violations)
    )


def test_no_bare_palace_in_branding_scope() -> None:
    """Second grep: bare 'palace' must not appear in modules/, behaviors/,
    skills/, context/, agents/, bundle.md, or README.md (D5: purge only the
    vendor/brand words; wing/room/drawer/diary/garden/mine stay)."""
    violations: list[tuple[str, int, str]] = []
    for path in _iter_repo_files():
        rel = path.relative_to(REPO_ROOT)
        parts = rel.parts
        in_scope_dir = parts and parts[0] in _PALACE_SCOPE_DIRS
        in_scope_file = str(rel) in _PALACE_SCOPE_FILES
        if not (in_scope_dir or in_scope_file):
            continue
        if path in _ALLOWLISTED_MEMPALACE_FILES:
            continue
        text = _read_text(path)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if _PALACE_RE.search(line):
                violations.append((str(rel), i, line.strip()[:120]))

    assert not violations, (
        "KG-N4 FAIL: bare 'palace' found in branding-scoped paths:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in violations)
    )
