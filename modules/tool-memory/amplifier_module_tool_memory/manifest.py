"""
Memory Capture Manifest — the "knowable list" of what memory captures.

This module externalises what was previously a hardcoded keyword table inside
the capture hook (``_CATEGORY_SIGNALS``) into a declarative, user-editable YAML
file. Both the hot-path capture hook and the cold-path consolidation pipeline
read the same manifest, so "what we capture" is a single knowable artifact.

Resolution order (first that parses wins):
    1. explicit ``config_path`` (from the hook's ``manifest_path`` config knob)
    2. ``<cwd>/project-context/memory-manifest.yaml``   (per-project override)
    3. ``<home>/.amplifier/memory-manifest.yaml``        (per-user default)
    4. the in-code ``DEFAULT_MANIFEST`` (mirrors ``context/memory-manifest.yaml``)

The in-code default reproduces the legacy hardcoded behavior exactly, so a
deployment with no manifest file behaves identically to before this change.

Pure module: no MCP calls, no network. YAML is parsed with PyYAML when present;
if PyYAML is unavailable, ``load_manifest`` degrades gracefully to the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # PyYAML ships with amplifier-core; degrade gracefully if absent.
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attractor:
    """One declared capture target.

    Attributes:
        id:              Stable category name; also used as the room suffix.
        seeds:           Lowercase substring signals used for keyword matching.
        importance_base: Base importance score in [0.0, 1.0], or None.
        intent:          Optional human description of what belongs here.
    """

    id: str
    seeds: tuple[str, ...]
    importance_base: float | None = None
    intent: str | None = None


@dataclass(frozen=True)
class Manifest:
    """A parsed capture manifest."""

    version: int
    attractors: tuple[Attractor, ...]
    emergent_enabled: bool = False
    emergent_promote_threshold: int = 5

    def category_signals(self) -> dict[str, list[str]]:
        """Return ``{category_id: [seed, ...]}`` preserving declaration order."""
        return {a.id: list(a.seeds) for a in self.attractors}

    def importance_bases(self) -> dict[str, float]:
        """Return ``{category_id: importance_base}`` excluding unset bases."""
        return {
            a.id: float(a.importance_base)
            for a in self.attractors
            if a.importance_base is not None
        }


# ---------------------------------------------------------------------------
# Category detection (zero-LLM, hot-path safe)
# ---------------------------------------------------------------------------


def detect_category(text: str, signals: dict[str, list[str]]) -> str | None:
    """Detect a category by substring keyword match.

    First category (in dict/declaration order) with any matching seed wins.
    Returns None when nothing matches.
    """
    lower = text.lower()
    for category, seeds in signals.items():
        if any(seed in lower for seed in seeds):
            return category
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Build a :class:`Manifest` from a plain dict (already-parsed YAML/JSON).

    Raises:
        ValueError: if an attractor is missing its required ``id``.
    """
    version = int(data.get("version", 1))

    attractors: list[Attractor] = []
    for raw in data.get("attractors") or []:
        if "id" not in raw or not raw["id"]:
            raise ValueError(f"attractor missing required 'id': {raw!r}")
        seeds = tuple(str(s) for s in (raw.get("seeds") or []))
        base = raw.get("importance_base")
        attractors.append(
            Attractor(
                id=str(raw["id"]),
                seeds=seeds,
                importance_base=None if base is None else float(base),
                intent=raw.get("intent"),
            )
        )

    emergent = data.get("emergent") or {}
    return Manifest(
        version=version,
        attractors=tuple(attractors),
        emergent_enabled=bool(emergent.get("enabled", False)),
        emergent_promote_threshold=int(emergent.get("promote_threshold", 5)),
    )


# ---------------------------------------------------------------------------
# Default manifest — mirrors context/memory-manifest.yaml and the legacy
# hardcoded keyword table + importance bases. Keep in sync with that file
# (TestBundledDefaultParity enforces parity).
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST = Manifest(
    version=1,
    attractors=(
        Attractor(
            "decision",
            ("decided", "decision", "we will", "going with", "chosen", "agreed"),
            0.75,
            "Decisions that shape what we build or how we build it",
        ),
        Attractor(
            "architecture",
            ("architecture", "design", "pattern", "structure", "component", "module"),
            0.70,
            "System structure, design patterns, component boundaries",
        ),
        Attractor(
            "blocker",
            ("blocked", "blocking", "cannot", "failed", "error", "issue", "problem"),
            0.65,
            "Active problems blocking progress",
        ),
        Attractor(
            "resolved_blocker",
            ("fixed", "resolved", "workaround", "solution found", "now works"),
            0.55,
            "Problems that were fixed, with the resolution",
        ),
        Attractor(
            "dependency",
            ("depends on", "requires", "dependency", "import", "package"),
            0.50,
            "What depends on what; external requirements",
        ),
        Attractor(
            "pattern",
            ("pattern", "convention", "always", "never", "best practice", "rule"),
            0.50,
            "Conventions and rules to follow or avoid",
        ),
        Attractor(
            "lesson_learned",
            ("learned", "lesson", "turns out", "discovered", "realized", "note:"),
            0.45,
            "Non-obvious things discovered the hard way",
        ),
    ),
    emergent_enabled=False,
    emergent_promote_threshold=5,
)


# ---------------------------------------------------------------------------
# Loading with resolution order + graceful fallback
# ---------------------------------------------------------------------------


def _candidate_paths(config_path: str | None, cwd: Path, home: Path) -> list[Path]:
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser())
    candidates.append(cwd / "project-context" / "memory-manifest.yaml")
    candidates.append(home / ".amplifier" / "memory-manifest.yaml")
    return candidates


def load_manifest(
    config_path: str | None = None,
    cwd: Path | str | None = None,
    home: Path | str | None = None,
) -> Manifest:
    """Resolve and load the manifest, falling back to :data:`DEFAULT_MANIFEST`.

    Never raises: any missing file, parse error, or missing PyYAML results in
    the in-code default (which mirrors the legacy hardcoded behavior).
    """
    if yaml is None:
        return DEFAULT_MANIFEST

    cwd_p = Path(cwd) if cwd is not None else Path.cwd()
    home_p = Path(home) if home is not None else Path.home()

    for path in _candidate_paths(config_path, cwd_p, home_p):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return parse_manifest(data)
        except Exception:
            # Malformed or unreadable — fall through to the next candidate,
            # and ultimately to the safe default.
            continue
    return DEFAULT_MANIFEST
