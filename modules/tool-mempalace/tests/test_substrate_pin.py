"""
KG-P1: the [substrate] extra resolves the pinned amplifier-data SHA.

Spins up an ephemeral venv, installs amplifier-data pinned exactly as
pyproject.toml's ``[project.optional-dependencies].substrate`` declares, and
verifies:
  (a) the installed distribution's recorded VCS commit_id matches the pin, and
  (b) ``callable(AmplifierStore().write_batch)`` is True on the installed
      package -- the atomic-update probe reports atomic on the direct backend.

Skipped when amplifier_data is not importable in THIS process (no substrate
available at all) or when ``uv`` is not on PATH (no fresh-venv install tool).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PYPROJECT = _REPO_ROOT / "modules" / "tool-mempalace" / "pyproject.toml"


def _pinned_dependency_spec() -> tuple[str, str]:
    """Return (git_url, sha) parsed from the substrate extra pin."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(
        r"amplifier-data @ git\+(https://[^@]+)@([0-9a-f]{40})",
        text,
    )
    assert m, "could not find pinned amplifier-data SHA in pyproject.toml"
    return m.group(1), m.group(2)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available for fresh-venv install")
def test_substrate_extra_resolves_pinned_sha(tmp_path: Path) -> None:
    git_url, sha = _pinned_dependency_spec()
    venv_dir = tmp_path / "pin-venv"

    created = subprocess.run(
        ["uv", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, f"uv venv failed:\n{created.stdout}\n{created.stderr}"

    venv_py = venv_dir / "bin" / "python"
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_py),
            f"amplifier-data @ git+{git_url}@{sha}",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert install.returncode == 0, f"install failed:\n{install.stdout}\n{install.stderr}"

    check = subprocess.run(
        [
            str(venv_py),
            "-c",
            (
                "import importlib.metadata as m\n"
                "d = m.Distribution.from_name('amplifier-data')\n"
                "print(d.read_text('direct_url.json'))\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    direct_url = json.loads(check.stdout)
    vcs_info = direct_url.get("vcs_info") or {}
    commit_id = vcs_info.get("commit_id", "")
    assert commit_id == sha, (
        f"installed amplifier-data commit_id {commit_id!r} does not match "
        f"the pinned SHA {sha!r} in {_PYPROJECT}"
    )

    probe = subprocess.run(
        [
            str(venv_py),
            "-c",
            (
                "from amplifier_data import AmplifierStore\n"
                "print(callable(AmplifierStore().write_batch))\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "True", (
        "the write_batch probe did not report atomic on the freshly-installed "
        f"direct backend: {probe.stdout!r} {probe.stderr!r}"
    )


def test_write_batch_callable_in_this_process() -> None:
    """Lightweight companion check: whatever amplifier-data is importable in
    THIS test process (editable or pinned), write_batch is callable -- the
    probe (memory_store._supports_atomic_update) reports atomic on the
    direct backend."""
    from amplifier_data import AmplifierStore

    assert callable(AmplifierStore().write_batch)
