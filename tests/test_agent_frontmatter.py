"""Agent files must not declare relative module sources (D25).

Foundation copies an agent file's ``tools:``/``hooks:`` entries verbatim and
resolves relative ``source:`` paths later against the base path of whichever
bundle was composed last -- not the agent file's own bundle. A relative source
in ``agents/*.md`` therefore breaks session start (strict activation) for any
consumer whose last-composed bundle is not this one.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _frontmatter(path: Path) -> dict:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(match.group(1)) or {}) if match else {}


def test_agent_module_sources_are_not_relative() -> None:
    offenders = []
    for path in sorted((REPO_ROOT / "agents").glob("*.md")):
        meta = _frontmatter(path)
        for section in ("tools", "hooks", "providers"):
            for entry in meta.get(section) or []:
                source = str((entry or {}).get("source", ""))
                if source.startswith((".", "/")) or source.startswith("modules/"):
                    offenders.append(f"{path.name}: {section} source {source!r}")
    assert not offenders, "relative module sources in agent files:\n" + "\n".join(offenders)
