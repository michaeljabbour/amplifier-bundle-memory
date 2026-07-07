"""
The capture hook must read category signals from the manifest (the knowable
list) instead of a hardcoded Python table. These are pure unit tests; the
sibling conftest puts tool-memory on sys.path so the runtime manifest import
resolves exactly as it would once installed.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_module_hooks_memory_capture import (
    MemoryCaptureHook,
    _detect_category,
)


def test_detect_category_legacy_default() -> None:
    # Backward compatible: no signals arg -> legacy keyword table.
    assert _detect_category("we decided to ship it") == "decision"
    assert _detect_category("the build failed with an error") == "blocker"


def test_detect_category_accepts_custom_signals() -> None:
    sig = {"weather": ["sunny", "rain"], "mood": ["happy"]}
    assert _detect_category("it is sunny today", sig) == "weather"
    assert _detect_category("i feel happy", sig) == "mood"
    assert _detect_category("totally unrelated", sig) is None


def test_hook_loads_signals_from_default_manifest() -> None:
    hook = MemoryCaptureHook(config={})
    sig = hook._signals
    assert isinstance(sig, dict)
    assert len(sig) > 0
    assert "decision" in sig  # default manifest mirrors the legacy categories


def test_hook_loads_signals_from_custom_manifest(tmp_path: Path) -> None:
    f = tmp_path / "m.yaml"
    f.write_text(
        "version: 1\nattractors:\n  - id: custom_cat\n    seeds: ['zzz']\n",
        encoding="utf-8",
    )
    hook = MemoryCaptureHook(config={"manifest_path": str(f)})
    assert "custom_cat" in hook._signals
    assert "decision" not in hook._signals


def test_hook_detects_with_loaded_signals(tmp_path: Path) -> None:
    f = tmp_path / "m.yaml"
    f.write_text(
        "version: 1\nattractors:\n  - id: custom_cat\n    seeds: ['zzz']\n",
        encoding="utf-8",
    )
    hook = MemoryCaptureHook(config={"manifest_path": str(f)})
    assert _detect_category("contains zzz token", hook._signals) == "custom_cat"
