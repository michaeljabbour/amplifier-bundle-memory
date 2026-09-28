"""Shared test fixtures/guards for the hooks-memory-briefing test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_memory_briefing(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No test may reach a real daemon via the mount-time background prefetch
    (tests needing one patch ``ensure_daemon`` themselves, after this runs)."""
    import amplifier_module_hooks_memory_briefing as briefing

    monkeypatch.setenv("AMPLIFIER_MEMORY_HOME", str(tmp_path / "memory-home"))
    monkeypatch.setattr(briefing, "ensure_daemon", lambda *a, **kw: None)
    briefing._reset_briefing_cache()
    yield
    briefing._wait_for_background(5.0)
    briefing._reset_briefing_cache()
