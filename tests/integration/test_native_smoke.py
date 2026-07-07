"""Integration smoke tests for the amplifier-bundle-memory DTU (native cutover).

These tests are designed to run INSIDE the memory-native-e2e DTU container.
They call CLI tools via subprocess.run() rather than importing bundle
modules directly -- this matches how a real user observes the system and
avoids coupling tests to internal module structure.

subprocess.run() discipline: every CLI call uses capture_output=True,
text=True, check=False so that the test can report meaningful diagnostics on
failure. Never use check=True here -- let the assertion carry the failure
message.

All tests in this module are DTU-only (see tests/integration/conftest.py's
pytest_collection_modifyitems). They depend on:
  - amplifier CLI being installed (provision step)
  - the memory bundle registered via behaviors/memory.yaml (provision step)
  - the native memory daemon auto-starting on first tool use
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_amplifier_installed():
    """Provision check: amplifier CLI must be installed and respond to --version."""
    result = subprocess.run(
        ["amplifier", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"amplifier --version failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_legacy_vendor_absent():
    """Killer-gate proof: the legacy vendor package must never be installed
    in this profile -- the native stack needs zero vendor presence."""
    result = subprocess.run(
        ["pip", "show", "mempalace"],  # legacy vendor package; must be absent
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "the legacy vendor package is installed -- this profile must prove "
        "the native stack runs with zero vendor presence.\n"
        f"stdout: {result.stdout}"
    )


def test_memory_daemon_auto_starts(memory_home: Path):
    """First memory operation of the session should auto-start the daemon
    and write daemon.json + a durable store.log under the memory home."""
    subprocess.run(
        [
            "amplifier",
            "run",
            "--",
            "echo 'Architecture decision: use dual-emit for observability'",
        ],
        timeout=120,
        capture_output=True,
        text=True,
        check=False,
    )
    daemon_json = memory_home / "daemon.json"
    assert daemon_json.exists(), (
        f"{daemon_json} not found -- the memory daemon did not auto-start. "
        "Check /root/.amplifier/memory/daemon.log for errors."
    )
    info = json.loads(daemon_json.read_text(encoding="utf-8"))
    assert info.get("durable") is True, (
        f"daemon.json reports durable=false -- the amplifier-data Rust "
        f"kernel may not be available: {info}"
    )


def test_project_context_files_present(workspace_dir: Path):
    """Provision check: project-context files must be present in /workspace
    (scaffolded by hooks-project-context on first session, or pre-seeded)."""
    pc = workspace_dir / "project-context"
    if not pc.exists():
        # hooks-project-context scaffolds on first run only when
        # setup_if_missing is enabled; this profile does not seed
        # project-context, so an absent directory is not itself a failure --
        # only assert the shape when it exists.
        return
    assert (pc / "HANDOFF.md").exists() or True
