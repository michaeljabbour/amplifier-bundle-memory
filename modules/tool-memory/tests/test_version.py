"""T6.7 (D28 follow-up 1): the source-level ``__version__`` constant that
``daemon.daemon_version()`` now reads MUST stay in lockstep with this
package's ``pyproject.toml`` ``[project].version`` -- a divergence here would
silently reintroduce the exact "daemon reports the wrong version" class of
bug this fix exists to close, just one layer up (source vs. packaging
metadata instead of source vs. dist-info).

Generalized to every lockstep module in this bundle that declares its own
``__version__`` constant (currently only tool-memory) so a future module
adopting the same pattern is covered without editing this test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_MODULES_DIR = Path(__file__).resolve().parents[2]


def _iter_version_constants() -> list[tuple[Path, str]]:
    """(module_dir, __version__ value) for every module under modules/ that
    declares a source-level ``__version__`` constant, checked either in
    ``<package>/_version.py`` (the preferred, import-cycle-safe location) or
    directly in ``<package>/__init__.py``."""
    found: list[tuple[Path, str]] = []
    for module_dir in sorted(_MODULES_DIR.iterdir()):
        if not module_dir.is_dir():
            continue
        for candidate in (
            module_dir.glob("amplifier_module_*/_version.py"),
            module_dir.glob("amplifier_module_*/__init__.py"),
        ):
            for path in candidate:
                text = path.read_text(encoding="utf-8")
                m = re.search(
                    r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE
                )
                if m:
                    found.append((module_dir, m.group(1)))
                    break  # one constant per module is enough to check
    return found


def test_pyproject_version_matches_source_version_constant() -> None:
    constants = _iter_version_constants()
    assert constants, (
        "expected at least one module to declare a __version__ constant "
        "(tool-memory, T6.7) -- if this fails the discovery glob broke, "
        "not that the lockstep rule is satisfied"
    )
    mismatches: list[str] = []
    for module_dir, const_version in constants:
        pyproject = module_dir / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        pyproject_version = data["project"]["version"]
        if pyproject_version != const_version:
            mismatches.append(
                f"{module_dir.name}: pyproject.toml version={pyproject_version!r} "
                f"!= __version__={const_version!r}"
            )
    assert not mismatches, "\n".join(mismatches)


def test_tool_memory_daemon_version_matches_constant() -> None:
    """``daemon_version()`` (T6.7) must resolve to the SAME string as the
    source constant it reads -- pinning the wiring, not just the constant."""
    from amplifier_module_tool_memory._version import __version__
    from amplifier_module_tool_memory.daemon import daemon_version

    assert daemon_version() == __version__
