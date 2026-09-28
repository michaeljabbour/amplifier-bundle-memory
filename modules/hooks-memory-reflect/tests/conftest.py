"""
Make sibling module packages importable for the reflect-hook unit tests.

pytest puts this module's own package dir on sys.path automatically, but the
reflect hook imports ``amplifier_module_tool_memory.*`` and (optionally)
``amplifier_module_hooks_memory_capture`` at runtime. In the dev tree
(modules are not pip-installed) we add the sibling module dirs so that
cross-module import resolves exactly as it would once installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _rel in (
    "modules/tool-memory",
    "modules/hooks-memory-capture",
    "modules/hooks-memory-reflect",
):
    _p = str(_REPO_ROOT / _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)
