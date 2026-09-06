"""
Memory Capture Manifest — the "knowable list" of what memory captures.

This module externalises what was previously a hardcoded keyword table inside
the capture hook (``_CATEGORY_SIGNALS``) into a declarative, user-editable YAML
file. Both the hot-path capture hook and the cold-path consolidation pipeline
read the same manifest, so "what we capture" is a single knowable artifact.

Resolution order (first that parses wins):
    1. explicit ``config_path`` (from the hook's ``manifest_path`` config knob)
    2. ``<cwd>/.amplifier/project-context/memory-manifest.yaml``  (per-project)
    3. ``<cwd>/project-context/memory-manifest.yaml``   (legacy per-project)
    4. ``<home>/.amplifier/memory-manifest.yaml``        (per-user default)
    5. the in-code ``DEFAULT_MANIFEST`` (mirrors ``context/memory-manifest.yaml``)

The in-code default mirrors the shipped ``context/memory-manifest.yaml`` exactly
(``TestBundledDefaultParity`` enforces id, seed, and importance parity), so a
deployment with no manifest file behaves the same as one that copies the
bundled file verbatim.

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
# Default manifest — mirrors context/memory-manifest.yaml. Keep in sync with
# that file (TestBundledDefaultParity enforces id, seed, and importance parity).
#
# Seeds are PHRASES, not bare words. Measured 2026-09-06 over 7 days of real
# sessions: single common words ("module", "design", "error", "import",
# "pattern") matched the body of almost every file read and shell result, so
# 8,330 drawers were filed in a week and 68% of them were raw tool output —
# 4,190 of those classified "architecture" because source code contains the
# word "module". Capture requires a category match (see the capture hook's
# `categories` filter), so seed precision is the capture filter.
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST = Manifest(
    version=1,
    attractors=(
        Attractor(
            "decision",
            (
                "we decided",
                "decision:",
                "we will use",
                "going with",
                "chose to",
                "agreed to",
                "opted for",
                "settled on",
            ),
            0.75,
            "Decisions that shape what we build or how we build it",
        ),
        Attractor(
            "architecture",
            (
                "design decision",
                "architectural",
                "component boundary",
                "module boundary",
                "the seam between",
                "layering rule",
                "how this fits together",
            ),
            0.70,
            "System structure, design patterns, component boundaries",
        ),
        Attractor(
            "blocker",
            (
                "blocked on",
                "blocked by",
                "is blocking",
                "cannot proceed",
                "root cause",
                "fails because",
                "reproduced with",
            ),
            0.65,
            "Active problems blocking progress",
        ),
        Attractor(
            "resolved_blocker",
            (
                "fixed by",
                "the fix was",
                "resolved by",
                "workaround:",
                "now passes",
                "works now because",
            ),
            0.55,
            "Problems that were fixed, with the resolution",
        ),
        Attractor(
            "dependency",
            (
                "depends on",
                "requires that",
                "hard dependency",
                "peer dependency",
                "pinned to",
                "must move in lockstep",
            ),
            0.50,
            "What depends on what; external requirements",
        ),
        Attractor(
            "pattern",
            (
                "the convention is",
                "always use",
                "never use",
                "the rule is",
                "anti-pattern",
                "best practice is",
            ),
            0.50,
            "Conventions and rules to follow or avoid",
        ),
        Attractor(
            "lesson_learned",
            (
                "turns out",
                "the lesson",
                "learned that",
                "discovered that",
                "counter-intuitively",
                "the surprising part",
            ),
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
    # Coordination files moved under the hidden per-repo directory; the legacy
    # top-level location is still honored so unmigrated repos keep working.
    candidates.append(cwd / ".amplifier" / "project-context" / "memory-manifest.yaml")
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
