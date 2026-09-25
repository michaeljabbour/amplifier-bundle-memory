"""Shared test fixtures/guards for the tool-memory test suite."""

from __future__ import annotations

from pathlib import Path


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

