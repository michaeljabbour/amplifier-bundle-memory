"""Integration test fixtures for the amplifier-bundle-memory DTU.

These tests run INSIDE the memory-native-e2e (or memory-migration-e2e) DTU
container. On the host machine they are automatically skipped -- see
pytest_collection_modifyitems.

Fixtures designed to run inside the DTU container:

- reset_memory_store: autouse module-scope fixture that best-effort resets
  the native memory store before each test module so each module starts
  from a clean slate.

- reset_project_context_workspace: autouse function-scope fixture that
  best-effort resets project-context/ in the workspace clone (git checkout)
  before every test in this suite. Prevents cross-test pollution: an
  earlier session's model can write to project-context/PROVENANCE.md (the
  bundle's own AGENTS.md instructs the agent to record decisions there),
  and a later session's model can then read that content and conclude a
  decision is "already documented" -- steering it away from an action a
  test depends on. Resetting before every test removes that cross-test
  dependency.

- workspace_dir: returns the Path to /workspace, the directory where
  Amplifier is launched inside the DTU. /workspace contains project-context/
  (the project context read by hooks-project-context) and
  amplifier-bundle-memory/ (the bundle under test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: Marker touched by the memory-native-e2e / memory-migration-e2e DTU
#: profiles during provision -- the native-cutover replacement for the old
#: sentinel (replaces the pre-cutover "does the legacy vendor directory
#: exist" check -- there is no such directory to key off of anymore).
_DTU_SENTINEL = Path("/root/.dtu-memory-native")

#: The git clone of this bundle inside the DTU container -- amplifier is
#: launched with this as cwd (see tests/integration/test_event_wiring.py's
#: WORKSPACE constant and test_native_smoke.py). project-context/ lives at
#: the root of this clone (it's tracked in this very repo), so a plain
#: `git checkout -- project-context/` from here is sufficient to reset it.
_BUNDLE_CLONE = Path("/workspace/amplifier-bundle-memory")


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


@pytest.fixture(autouse=True)
def reset_project_context_workspace():
    """Reset project-context/ in the workspace clone before every test.

    Best-effort and tolerant of every reason this could fail to apply:
    missing git binary, _BUNDLE_CLONE not existing yet (e.g. tests run
    before provisioning finished), project-context/ absent, or the clone
    not being a git repo. A test should never fail because hygiene
    couldn't run -- only because the thing it actually tests failed.
    """
    if not _BUNDLE_CLONE.is_dir():
        yield
        return
    pc_dir = _BUNDLE_CLONE / "project-context"
    if pc_dir.is_dir():
        try:
            subprocess.run(
                ["git", "checkout", "--", "project-context/"],
                cwd=_BUNDLE_CLONE,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
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
