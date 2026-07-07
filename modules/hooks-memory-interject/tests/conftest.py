"""
Make sibling module packages importable for the interject-hook unit tests.

pytest puts this module's own package dir on sys.path automatically, but the
interject hook imports ``amplifier_module_tool_memory.scripts.memory_store``
at runtime (for the canonical ``_call_mcp_tool`` helper). In the dev tree
(modules are not pip-installed) we add the sibling module dir so cross-module
import resolves exactly as it would once installed. Mirrors the identical
conftest.py in hooks-memory-capture/tests/.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _rel in ("modules/tool-memory", "modules/hooks-memory-interject"):
    _p = str(_REPO_ROOT / _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_legacy_vendor_not_installed_in_this_environment() -> None:
    """Native cutover killer gate (docs/plans/2026-07-07-native-cutover-
    design.md): ALL per-module suites must run with the legacy vendor
    package ABSENT from the venv -- there is no vendor subprocess anywhere
    in this codebase anymore."""
    import importlib.util

    assert importlib.util.find_spec("mempalace") is None, (
        "the legacy vendor package is installed in this test environment -- "
        "the native cutover gate requires every per-module suite to run "
        "with it absent."
    )
