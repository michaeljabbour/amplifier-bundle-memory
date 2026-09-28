"""Shared automation opt-out (perf/incremental-fold, part B).

Automated/non-interactive sessions -- most concretely the fast-decisions
benchmark harness (``~/dev/amplifier-bundle-fast-decisions``), which can run
thousands of ``amplifier run`` subprocesses a month, each writing a memory
capture after every tool call -- have no use for personal long-term memory
and no one reads their captures. Each one still pays the write cost (a
daemon round trip per tool call) and pollutes the store with automation
noise that then costs every REAL session extra fold-materialization work
forever after (the store only grows).

This module is the ONE place that decides "is memory automation active for
this process", so ``hooks-memory-capture``, ``hooks-memory-interject``, and
``hooks-memory-briefing`` (the three consumers) can never disagree about it.
Two independent, additive opt-out signals:

* ``AMPLIFIER_MEMORY_CAPTURE`` environment variable -- set to ``off``
  (or ``0``/``false``/``no``, case-insensitive) to disable memory writes and
  reads for every session in that process tree. Meant for automation
  launchers (a bench harness, a CI runner) to set once for every child
  process it spawns, without touching any repo's committed config.
* ``excluded_working_dirs`` config list (per hook, in ``behaviors/memory.yaml``
  or a project's own bundle config) -- glob patterns (``fnmatch`` syntax)
  matched against the process's current working directory. Meant for a
  fixed, known automation root (e.g. a benchmark harness's experiments/
  tree) that always runs from specific directories, with no environment
  variable required.

Default behavior for a normal interactive session (neither signal set) is
COMPLETELY UNCHANGED -- :func:`automation_opt_out` returns ``False``.
"""

from __future__ import annotations

import fnmatch
import os

__all__ = ["CAPTURE_ENV_VAR", "automation_opt_out"]

#: Environment variable name (documented in each hook's README/CHANGELOG and
#: in behaviors/memory.yaml). Any of "off"/"0"/"false"/"no" (case-insensitive,
#: surrounding whitespace ignored) opts out; anything else -- including
#: unset -- does not.
CAPTURE_ENV_VAR = "AMPLIFIER_MEMORY_CAPTURE"

_OFF_VALUES = frozenset({"off", "0", "false", "no"})


def automation_opt_out(
    *, excluded_working_dirs: list[str] | None = None, cwd: str | None = None
) -> bool:
    """``True`` when this process has opted out of memory capture/interject/
    briefing -- via ``AMPLIFIER_MEMORY_CAPTURE`` or a matching
    ``excluded_working_dirs`` glob. Never raises.

    *cwd* is only for tests; production callers always let it default to
    ``os.getcwd()`` (the actual per-process working directory the launcher
    set, e.g. a bench harness's per-experiment run directory).
    """
    try:
        env_value = os.environ.get(CAPTURE_ENV_VAR, "").strip().lower()
        if env_value in _OFF_VALUES:
            return True
        if excluded_working_dirs:
            resolved = cwd if cwd is not None else os.getcwd()
            for pattern in excluded_working_dirs:
                if fnmatch.fnmatch(resolved, pattern):
                    return True
        return False
    except Exception:  # noqa: BLE001 -- an opt-out check must never crash a hook
        return False
