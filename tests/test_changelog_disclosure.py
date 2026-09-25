"""2.0.2 must disclose its deliberate behavior changes as behavior changes.

Independent review MUST-FIX 2: the briefing-delivery change was filed under
"Fixed" only, without its token cost, its persistence under the default
orchestrator, the sub-session default, the 300 s cache staleness, or the
automatic 2.0.1 -> 2.0.2 daemon replacement.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _release_notes(version: str) -> str:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index(f"## [{version}]")
    end = text.find("\n## [", start + 1)
    return text[start : end if end != -1 else None]


def test_202_has_a_changed_section_disclosing_each_behavior_change() -> None:
    notes = _release_notes("2.0.2")
    changed = notes[notes.index("### Changed") : notes.index("### Fixed")]
    for needle in (
        "1,500 tokens",  # token cost
        "ephemeral_injection_mode: persist",  # kept in history by default
        "brief_subsessions",  # sub-agent sessions skipped
        "cache_ttl_s",  # memory sections up to 5 min old
        "retires a running 2.0.1 daemon",  # automatic daemon replacement
    ):
        assert needle in changed, needle


def test_behavior_yaml_and_briefing_readme_mirror_the_disclosure() -> None:
    yaml_text = (REPO_ROOT / "behaviors" / "memory.yaml").read_text(encoding="utf-8")
    readme = (
        REPO_ROOT / "modules" / "hooks-memory-briefing" / "README.md"
    ).read_text(encoding="utf-8")
    for text in (yaml_text, readme):
        assert "ephemeral_injection_mode: persist" in text
        assert "brief_subsessions" in text
        assert "1,500 tokens" in text
