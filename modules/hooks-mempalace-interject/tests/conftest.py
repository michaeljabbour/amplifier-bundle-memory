"""
Make sibling module packages importable for the interject-hook unit tests.

pytest puts this module's own package dir on sys.path automatically, but the
interject hook imports ``amplifier_module_tool_mempalace.scripts.memory_store``
at runtime (for the canonical ``_call_mcp_tool`` helper). In the dev tree
(modules are not pip-installed) we add the sibling module dir so cross-module
import resolves exactly as it would once installed. Mirrors the identical
conftest.py in hooks-mempalace-capture/tests/.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _rel in ("modules/tool-mempalace", "modules/hooks-mempalace-interject"):
    _p = str(_REPO_ROOT / _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)
