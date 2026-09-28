"""
Bundle-level test configuration.

Adds all module directories to sys.path so hook modules can be imported
without full package installation. The try/except imports in each hook handle
the missing amplifier_core Hook/HookContext gracefully.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MODULES_DIR = REPO_ROOT / "modules"

# Prepend each module source dir so Python finds the packages
for mod_dir in sorted(MODULES_DIR.iterdir()):
    if mod_dir.is_dir():
        sys.path.insert(0, str(mod_dir))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_memory_briefing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep the briefing hook's mount-time background prefetch hermetic.

    ``hooks-memory-briefing``'s ``mount()`` starts its daemon lookups on a
    background thread. Without this, any test that mounts it would discover
    (or spawn) a real memory daemon under the developer's home. Tests that
    need a daemon patch ``ensure_daemon`` themselves -- their ``setattr``
    runs after this fixture and wins.
    """
    monkeypatch.setenv("AMPLIFIER_MEMORY_HOME", str(tmp_path / "memory-home"))
    try:
        import amplifier_module_hooks_memory_briefing as briefing
    except ImportError:
        yield
        return
    monkeypatch.setattr(briefing, "ensure_daemon", lambda *a, **kw: None)
    briefing._reset_briefing_cache()
    yield
    briefing._wait_for_background(5.0)
    briefing._reset_briefing_cache()
