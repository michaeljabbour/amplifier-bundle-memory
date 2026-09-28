"""
Make sibling module packages importable for the capture-hook unit tests.

pytest puts this module's own package dir on sys.path automatically, but the
capture hook imports ``amplifier_module_tool_memory.manifest`` at runtime.
In the dev tree (modules are not pip-installed) we add the sibling module dir
so that cross-module import resolves exactly as it would once installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _rel in ("modules/tool-memory", "modules/hooks-memory-capture"):
    _p = str(_REPO_ROOT / _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_memory_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the default memory home (``AMPLIFIER_MEMORY_HOME``) at a tmp dir.

    ``event_emitter.emit_event`` always writes to the DEFAULT home
    (``default_memory_home()`` -> ``~/.amplifier/memory/events``), even when
    the caller works on an explicit home: e.g. ``client.ensure_daemon(tmp)``
    emits ``daemon_spawned`` there. Without this, tests left
    ``pid_*.jsonl`` files in the developer's real events directory. Tests
    that need a specific home still set their own (their ``setenv`` /
    ``setattr`` runs after this fixture and wins).
    """
    monkeypatch.setenv("AMPLIFIER_MEMORY_HOME", str(tmp_path / "memory-home"))
    yield
