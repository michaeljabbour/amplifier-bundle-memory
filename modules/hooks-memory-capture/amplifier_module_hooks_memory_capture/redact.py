"""Thin re-export shim (P2 redact relocation).

The real implementation now lives in ``amplifier_module_tool_memory.redact``
(shared with ``fact_add``'s secret gate). This shim keeps
``from amplifier_module_hooks_memory_capture.redact import redact`` working
unchanged for this module's own callers and tests -- tool-memory is already
a sibling dependency of hooks-memory-capture (see pyproject.toml / conftest.py
sys.path wiring), so this import always resolves.
"""

from __future__ import annotations

from amplifier_module_tool_memory.redact import redact

__all__ = ["redact"]
