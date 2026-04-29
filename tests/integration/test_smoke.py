"""Integration smoke tests for the amplifier-bundle-memory DTU.

These tests are designed to run INSIDE the DTU container. They call CLI tools
via subprocess.run() rather than importing bundle modules directly — this
matches how a real user observes the system.
"""

from __future__ import annotations


def test_workspace_dir_fixture(workspace_dir):
    # Probe that the conftest provides the workspace_dir fixture.
    assert str(workspace_dir) == "/workspace"
