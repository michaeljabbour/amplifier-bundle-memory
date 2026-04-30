"""Integration test fixtures for the amplifier-bundle-memory DTU.

These fixtures are designed to run INSIDE the DTU container:

- reset_palace: autouse module-scope fixture that resets the memory palace
  before each test module so each module starts with a clean slate. Calls the
  ``reset-palace`` CLI tool that is installed in the DTU environment.

- workspace_dir: returns the Path to /workspace, the directory where Amplifier
  is launched inside the DTU. /workspace contains project-context/ (the project
  context read by hooks-project-context) and amplifier-bundle-memory/ (the
  bundle under test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def reset_palace():
    """Reset the memory palace before each test module.

    Runs the ``reset-palace`` CLI tool which is installed in the DTU
    environment. If the command fails (e.g. when running on the host rather
    than inside the DTU), the fixture calls pytest.fail() with the returncode
    and stderr so the error is immediately visible.

    Yields control to the test module after the reset completes.
    """
    result = subprocess.run(
        ["reset-palace"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"reset-palace failed (rc={result.returncode}).\n"
            f"stderr: {result.stderr}\n"
            "Note: this fixture only runs correctly inside the DTU container."
        )
    yield


@pytest.fixture
def workspace_dir() -> Path:
    """/workspace — the directory where Amplifier is launched inside the DTU.

    Contains:
    - project-context/   read by hooks-project-context
    - amplifier-bundle-memory/  the bundle under test
    """
    return Path("/workspace")
