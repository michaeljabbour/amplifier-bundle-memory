"""Single source of truth for this package's version (T6.7, D28 follow-up 1).

``daemon.daemon_version()`` and the daemon's ``/health`` payload read this
constant directly instead of ``importlib.metadata`` -- installed dist-info
is only refreshed by a package reinstall, but a bundle cache refresh updates
the SOURCE tree in place without reinstalling the editable module. That left
a daemon running old code reporting an old (correct-at-the-time) dist-info
version indefinitely after an in-place source update. This constant is a
plain source-level assignment, so it changes the instant this file's bytes
change on disk -- exactly in step with the rest of the package's source.

No imports here on purpose: both ``__init__.py`` (which needs the whole
package importable, including ``amplifier_core`` and other dependencies) and
``daemon.py`` (imported standalone via ``python -m
amplifier_module_tool_memory.daemon`` without necessarily importing the
package's ``__init__.py`` first) need to read this value without triggering
either side's import graph -- see the circular-import note in ``__init__.py``
and ``daemon.py``.

Keep this in lockstep with ``[project].version`` in this module's
``pyproject.toml`` -- ``tests/test_version.py`` asserts equality.
"""

from __future__ import annotations

__version__ = "2.2.0"

__all__ = ["__version__"]
