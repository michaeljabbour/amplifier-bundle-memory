"""Integration smoke tests for the amplifier-bundle-memory DTU.

These tests are designed to run INSIDE the DTU container. They call CLI tools
via subprocess.run() rather than importing bundle modules directly — this
matches how a real user observes the system and avoids coupling tests to
internal module structure.

subprocess.run() discipline: every CLI call uses capture_output=True,
text=True, check=False so that the test can report meaningful diagnostics on
failure. Never use check=True here — let the assertion carry the failure
message.

All 7 tests in this module are DTU-only. They depend on:
  - /root/.mempalace and /root/.mempalace-seed existing (DTU provision step)
  - mempalace CLI being installed (provision step)
  - amplifier CLI being installed (provision step)
  - reset-palace CLI being installed (provision step)
  - /workspace/project-context/ being populated (provision step 8)

Running on a host machine without the DTU will cause all tests to fail or
error at the autouse reset_palace fixture.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_palace_directory_exists():
    """Provision check: both memory-palace directories must exist.

    /root/.mempalace      — created by the DTU provision step (palace init)
    /root/.mempalace-seed — created by the DTU provision step (seed install)
    """
    assert Path("/root/.mempalace").exists(), (
        "/root/.mempalace does not exist — the DTU provision step that "
        "initialises the memory palace failed or was not run."
    )
    assert Path("/root/.mempalace-seed").exists(), (
        "/root/.mempalace-seed does not exist — the DTU provision step that "
        "installs the seed content failed or was not run."
    )


def test_mempalace_installed():
    """Provision check: mempalace CLI must be installed and respond to --version."""
    result = subprocess.run(
        ["mempalace", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"mempalace --version failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_palace_has_seeded_drawers():
    """After seeding, mempalace status must report at least one drawer.

    Uses `mempalace status` which outputs human-readable text.
    Expected format: 'N drawers' where N > 0.
    """
    result = subprocess.run(
        ["mempalace", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"mempalace status failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # The output contains "N drawers" — parse the count from the summary line.
    # Example: "MemPalace Status — 19 drawers"
    match = re.search(r"(\d+)\s+drawer", result.stdout)
    assert match is not None, (
        "mempalace status output does not contain a drawer count — "
        "seed content may not have been loaded.\n"
        f"output: {result.stdout}"
    )
    drawer_count = int(match.group(1))
    assert drawer_count > 0, (
        f"mempalace status reports zero drawers — seed content was not loaded.\n"
        f"output: {result.stdout}"
    )


def test_seed_content_searchable():
    """Seed content must be searchable via mempalace search.

    Uses `mempalace search <query> --results 3` which outputs human-readable
    text. A non-empty result block indicates the seed content is searchable.
    """
    result = subprocess.run(
        ["mempalace", "search", "architecture decisions mempalace", "--results", "3"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"mempalace search failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # The output contains "[1]" result markers when results are found.
    assert "[1]" in result.stdout, (
        "mempalace search returned no results for 'architecture decisions mempalace' "
        "— seed content is missing or search is broken.\n"
        f"output: {result.stdout}"
    )


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


def test_reset_palace_restores_seed():
    """reset-palace must replace the palace directory and restore seed content.

    Writes a sentinel file into the palace, calls reset-palace, then asserts:
    1. The sentinel is gone (palace was actually replaced, not just patched).
    2. drawer_count > 0 after reset (seed content was restored).
    """
    sentinel = Path("/root/.mempalace/sentinel_test.txt")
    sentinel.write_text("dirty")
    assert sentinel.exists(), (
        "Failed to write sentinel file — /root/.mempalace may not exist."
    )

    reset_result = subprocess.run(
        ["reset-palace"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert reset_result.returncode == 0, (
        f"reset-palace failed (rc={reset_result.returncode}).\n"
        f"stdout: {reset_result.stdout}\n"
        f"stderr: {reset_result.stderr}"
    )

    assert not sentinel.exists(), (
        "Sentinel survived reset — palace was not actually replaced."
    )

    status_result = subprocess.run(
        ["mempalace", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status_result.returncode == 0, (
        f"mempalace status after reset failed (rc={status_result.returncode}).\n"
        f"stdout: {status_result.stdout}\n"
        f"stderr: {status_result.stderr}"
    )
    match = re.search(r"(\d+)\s+drawer", status_result.stdout)
    assert match is not None and int(match.group(1)) > 0, (
        "After reset, palace has zero drawers — seed restore is broken.\n"
        f"output: {status_result.stdout}"
    )


def test_project_context_files_present(workspace_dir):
    """Provision check: project-context files must be present in /workspace.

    Expects provision step 8 to have populated:
      /workspace/project-context/HANDOFF.md
      /workspace/project-context/PROJECT_CONTEXT.md
      /workspace/project-context/GLOSSARY.md
    """
    pc = workspace_dir / "project-context"
    assert (pc / "HANDOFF.md").exists(), (
        f"{pc / 'HANDOFF.md'} not found — "
        "the DTU provision step 8 that writes project-context files failed or was not run."
    )
    assert (pc / "PROJECT_CONTEXT.md").exists(), (
        f"{pc / 'PROJECT_CONTEXT.md'} not found — "
        "project-context was not fully provisioned."
    )
    assert (pc / "GLOSSARY.md").exists(), (
        f"{pc / 'GLOSSARY.md'} not found — project-context was not fully provisioned."
    )
