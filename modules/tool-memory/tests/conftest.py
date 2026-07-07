"""Shared test fixtures/guards for the tool-memory test suite."""

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
