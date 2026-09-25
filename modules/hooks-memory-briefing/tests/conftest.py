"""Shared test fixtures/guards for the hooks-memory-briefing test suite."""

from __future__ import annotations


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
