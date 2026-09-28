"""Shared test fixtures/guards for the tool-memory test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


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
