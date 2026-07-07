"""Integration test fixtures for the amplifier-bundle-memory DTU.

These tests run INSIDE the memory-native-e2e (or memory-migration-e2e) DTU
container. On the host machine they are automatically skipped -- see
pytest_collection_modifyitems.

Fixtures designed to run inside the DTU container:

- reset_memory_store: autouse module-scope fixture that best-effort resets
  the native memory store before each test module so each module starts
  from a clean slate.

- workspace_dir: returns the Path to /workspace, the directory where
  Amplifier is launched inside the DTU. /workspace contains project-context/
  (the project context read by hooks-project-context) and
  amplifier-bundle-memory/ (the bundle under test).
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Marker touched by the memory-native-e2e / memory-migration-e2e DTU
#: profiles during provision -- the native-cutover replacement for the old
#: sentinel (replaces the pre-cutover "does the legacy vendor directory
#: exist" check -- there is no such directory to key off of anymore).
_DTU_SENTINEL = Path("/root/.dtu-memory-native")


def pytest_collection_modifyitems(config, items):
    """Skip all integration tests when not running inside a native-cutover
    DTU container.

    A PermissionError is treated as "not in DTU" -- it means we are running
    as a non-root user that cannot stat the sentinel path.
    """
    try:
        in_dtu = _DTU_SENTINEL.exists()
    except PermissionError:
        in_dtu = False

    if in_dtu:
        return

    skip_marker = pytest.mark.skip(
        reason="DTU environment required (run inside memory-native-e2e container)"
    )
    for item in items:
        item.add_marker(skip_marker)


@pytest.fixture(scope="module", autouse=True)
def reset_memory_store():
    """Best-effort reset of the native memory store before each test module.

    Unlike the pre-cutover fixture (which shelled out to a legacy reset
    script), the native store is a durable amplifier-data log with no
    equivalent CLI reset tool shipped by this bundle. This fixture is a
    best-effort no-op placeholder: if a future DTU profile provisions a
    reset script it can be wired in here. Tests should not assume a clean
    store between modules; they scope by wing/room instead.
    """
    yield


@pytest.fixture
def workspace_dir() -> Path:
    """/workspace -- the directory where Amplifier is launched inside the DTU.

    Contains:
    - project-context/   read by hooks-project-context
    - amplifier-bundle-memory/  the bundle under test
    """
    return Path("/workspace")


@pytest.fixture
def memory_home() -> Path:
    """~/.amplifier/memory -- the native memory home directory inside the DTU."""
    return Path("/root/.amplifier/memory")
