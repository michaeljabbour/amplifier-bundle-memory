"""
Tests for amplifier_module_tool_memory.manifest

The manifest is the "knowable list": a declarative description of what memory
captures, replacing the hardcoded keyword table in the capture hook. These are
pure unit tests — no MCP calls, no store fixture, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_module_tool_memory.manifest import (
    DEFAULT_MANIFEST,
    Attractor,
    Manifest,
    detect_category,
    load_manifest,
    parse_manifest,
)

# ---------------------------------------------------------------------------
# DEFAULT_MANIFEST — must reproduce the legacy hardcoded behavior exactly
# ---------------------------------------------------------------------------

_LEGACY_BASES = {
    "decision": 0.75,
    "architecture": 0.70,
    "blocker": 0.65,
    "resolved_blocker": 0.55,
    "dependency": 0.50,
    "pattern": 0.50,
    "lesson_learned": 0.45,
}


class TestDefaultManifest:
    def test_default_has_seven_attractors(self) -> None:
        assert len(DEFAULT_MANIFEST.attractors) == 7

    def test_default_ids_in_legacy_order(self) -> None:
        ids = [a.id for a in DEFAULT_MANIFEST.attractors]
        assert ids == list(_LEGACY_BASES.keys())

    def test_default_importance_bases_match_legacy(self) -> None:
        assert DEFAULT_MANIFEST.importance_bases() == pytest.approx(_LEGACY_BASES)

    def test_default_category_signals_shape(self) -> None:
        sig = DEFAULT_MANIFEST.category_signals()
        assert sig["decision"][0] == "decided"
        assert "turns out" in sig["lesson_learned"]
        assert all(isinstance(v, list) for v in sig.values())

    def test_default_emergent_disabled(self) -> None:
        assert DEFAULT_MANIFEST.emergent_enabled is False
        assert DEFAULT_MANIFEST.emergent_promote_threshold == 5


# ---------------------------------------------------------------------------
# detect_category — keyword matching against a manifest's signals
# ---------------------------------------------------------------------------


class TestDetectCategory:
    def test_detects_decision(self) -> None:
        sig = DEFAULT_MANIFEST.category_signals()
        assert detect_category("We decided to go with option A", sig) == "decision"

    def test_detects_blocker(self) -> None:
        sig = DEFAULT_MANIFEST.category_signals()
        assert detect_category("This failed with an error", sig) == "blocker"

    def test_no_match_returns_none(self) -> None:
        sig = DEFAULT_MANIFEST.category_signals()
        assert detect_category("the quick brown fox jumps", sig) is None

    def test_first_match_wins_in_order(self) -> None:
        # "design pattern" hits architecture before pattern (architecture first)
        sig = DEFAULT_MANIFEST.category_signals()
        assert detect_category("a nice design here", sig) == "architecture"

    def test_case_insensitive(self) -> None:
        sig = DEFAULT_MANIFEST.category_signals()
        assert detect_category("DECISION made", sig) == "decision"

    def test_custom_signals(self) -> None:
        sig = {"weather": ["sunny", "rain"], "mood": ["happy"]}
        assert detect_category("it is sunny today", sig) == "weather"
        assert detect_category("i am happy", sig) == "mood"
        assert detect_category("nothing here", sig) is None


# ---------------------------------------------------------------------------
# parse_manifest — dict -> Manifest
# ---------------------------------------------------------------------------


class TestParseManifest:
    def test_parse_minimal(self) -> None:
        data = {
            "version": 1,
            "attractors": [
                {"id": "foo", "seeds": ["a", "b"], "importance_base": 0.9},
            ],
        }
        m = parse_manifest(data)
        assert isinstance(m, Manifest)
        assert m.version == 1
        assert len(m.attractors) == 1
        a = m.attractors[0]
        assert isinstance(a, Attractor)
        assert a.id == "foo"
        assert a.seeds == ("a", "b")
        assert a.importance_base == 0.9

    def test_parse_emergent(self) -> None:
        data = {
            "version": 1,
            "attractors": [],
            "emergent": {"enabled": True, "promote_threshold": 3},
        }
        m = parse_manifest(data)
        assert m.emergent_enabled is True
        assert m.emergent_promote_threshold == 3

    def test_parse_missing_emergent_defaults(self) -> None:
        m = parse_manifest({"version": 1, "attractors": []})
        assert m.emergent_enabled is False
        assert m.emergent_promote_threshold == 5

    def test_parse_attractor_without_importance(self) -> None:
        m = parse_manifest({"version": 1, "attractors": [{"id": "x", "seeds": ["q"]}]})
        assert m.attractors[0].importance_base is None
        assert m.importance_bases() == {}  # None bases excluded

    def test_parse_rejects_attractor_without_id(self) -> None:
        with pytest.raises((KeyError, ValueError)):
            parse_manifest({"version": 1, "attractors": [{"seeds": ["q"]}]})


# ---------------------------------------------------------------------------
# load_manifest — resolution order + graceful fallback
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def test_no_files_returns_default(self, tmp_path: Path) -> None:
        m = load_manifest(config_path=None, cwd=tmp_path, home=tmp_path)
        assert m is DEFAULT_MANIFEST

    def test_explicit_config_path_wins(self, tmp_path: Path) -> None:
        f = tmp_path / "custom.yaml"
        f.write_text(
            "version: 1\nattractors:\n  - id: only\n    seeds: ['z']\n    importance_base: 0.3\n",
            encoding="utf-8",
        )
        m = load_manifest(config_path=str(f), cwd=tmp_path, home=tmp_path)
        assert [a.id for a in m.attractors] == ["only"]

    def test_project_context_preferred_over_home(self, tmp_path: Path) -> None:
        cwd = tmp_path / "proj"
        home = tmp_path / "home"
        (cwd / "project-context").mkdir(parents=True)
        home.mkdir(parents=True)
        (cwd / "project-context" / "memory-manifest.yaml").write_text(
            "version: 1\nattractors:\n  - id: proj_win\n    seeds: ['a']\n",
            encoding="utf-8",
        )
        (home / ".amplifier").mkdir(parents=True)
        (home / ".amplifier" / "memory-manifest.yaml").write_text(
            "version: 1\nattractors:\n  - id: home_lose\n    seeds: ['b']\n",
            encoding="utf-8",
        )
        m = load_manifest(config_path=None, cwd=cwd, home=home)
        assert [a.id for a in m.attractors] == ["proj_win"]

    def test_home_used_when_no_project(self, tmp_path: Path) -> None:
        cwd = tmp_path / "proj"
        home = tmp_path / "home"
        cwd.mkdir(parents=True)
        (home / ".amplifier").mkdir(parents=True)
        (home / ".amplifier" / "memory-manifest.yaml").write_text(
            "version: 1\nattractors:\n  - id: home_win\n    seeds: ['b']\n",
            encoding="utf-8",
        )
        m = load_manifest(config_path=None, cwd=cwd, home=home)
        assert [a.id for a in m.attractors] == ["home_win"]

    def test_malformed_yaml_falls_back_to_default(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("version: 1\nattractors: [this is : : not valid", encoding="utf-8")
        m = load_manifest(config_path=str(f), cwd=tmp_path, home=tmp_path)
        assert m is DEFAULT_MANIFEST

    def test_missing_explicit_path_falls_back(self, tmp_path: Path) -> None:
        m = load_manifest(
            config_path=str(tmp_path / "does-not-exist.yaml"),
            cwd=tmp_path,
            home=tmp_path,
        )
        assert m is DEFAULT_MANIFEST


# ---------------------------------------------------------------------------
# Bundled default file parity — context/memory-manifest.yaml must parse and
# match the in-code DEFAULT_MANIFEST (so the editable file and the fallback
# never drift).
# ---------------------------------------------------------------------------


class TestBundledDefaultParity:
    def test_repo_default_matches_code_default(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        default_file = repo_root / "context" / "memory-manifest.yaml"
        if not default_file.exists():
            pytest.skip("repo default manifest not present")
        m = load_manifest(config_path=str(default_file))
        assert [a.id for a in m.attractors] == [
            a.id for a in DEFAULT_MANIFEST.attractors
        ]
        assert m.importance_bases() == pytest.approx(
            DEFAULT_MANIFEST.importance_bases()
        )
