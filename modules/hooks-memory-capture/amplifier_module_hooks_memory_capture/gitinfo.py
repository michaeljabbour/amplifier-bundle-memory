"""Subprocess-free git HEAD commit resolution (T0.3 support for D8's
``at_commit`` provenance fact).

Walks up from a starting directory looking for a ``.git`` entry (a plain
directory for a normal repo, or a ``gitdir: <path>`` file for a worktree),
then reads ``HEAD`` directly off disk -- no ``git`` subprocess. Results are
cached per ``(git_dir, HEAD mtime)`` so repeated calls in a hot loop (one
per capture) are cheap.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["resolve_commit"]

_CACHE: dict[tuple[str, float], str | None] = {}


def _find_git_dir(start: Path) -> Path | None:
    """Walk up from *start* looking for ``.git`` (dir or worktree file)."""
    cur = start.resolve()
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        candidate = cur / ".git"
        if candidate.is_dir():
            return candidate
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                raw = text.split(":", 1)[1].strip()
                gitdir = Path(raw)
                if not gitdir.is_absolute():
                    gitdir = (cur / gitdir).resolve()
                return gitdir if gitdir.exists() else None
            return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def _read_ref(git_dir: Path, ref: str) -> str | None:
    """Resolve a symbolic ref (e.g. ``refs/heads/main``) to a sha."""
    ref_path = git_dir / ref
    if ref_path.exists():
        try:
            return ref_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    packed = git_dir / "packed-refs"
    if not packed.exists():
        return None
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#^":
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        return None
    return None


def resolve_commit(start_path: str | os.PathLike[str] | None = None) -> str | None:
    """Return the HEAD commit sha for the git repo containing *start_path*.

    ``start_path`` defaults to the current working directory. Returns
    ``None`` outside a git repo, or if any expected git-internal file is
    missing/unreadable. Never raises and never shells out.
    """
    start = Path(start_path) if start_path is not None else Path.cwd()
    git_dir = _find_git_dir(start)
    if git_dir is None:
        return None

    head_path = git_dir / "HEAD"
    try:
        mtime = head_path.stat().st_mtime
    except OSError:
        return None

    cache_key = (str(git_dir), mtime)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    try:
        head_text = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        _CACHE[cache_key] = None
        return None

    result: str | None
    if head_text.startswith("ref:"):
        ref = head_text.split(":", 1)[1].strip()
        result = _read_ref(git_dir, ref)
    else:
        # Detached HEAD -- HEAD holds the sha directly.
        result = head_text or None

    _CACHE[cache_key] = result
    return result
