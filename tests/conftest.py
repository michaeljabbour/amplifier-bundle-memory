"""
Bundle-level test configuration.

Adds all module directories to sys.path so hook modules can be imported
without full package installation. The try/except imports in each hook handle
the missing amplifier_core Hook/HookContext gracefully.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MODULES_DIR = REPO_ROOT / "modules"

# Prepend each module source dir so Python finds the packages
for mod_dir in sorted(MODULES_DIR.iterdir()):
    if mod_dir.is_dir():
        sys.path.insert(0, str(mod_dir))
