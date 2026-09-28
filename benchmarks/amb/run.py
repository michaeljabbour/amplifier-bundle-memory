"""Runner: registers ``AmplifierMemoryProvider`` into AMB's own registry at
runtime, then hands off to AMB's real CLI (``memory_bench.cli:app``).

AMB (github.com/vectorize-io/agent-memory-benchmark) ships NO LICENSE file,
so it is never vendored -- this script only imports an externally installed
or cloned copy (see README.md) and mutates its module-level ``REGISTRY``
dict in place. Everything after registration is AMB's own, unmodified code.

Usage (passthrough args are AMB's own ``amb run`` flags -- see README.md):

    python benchmarks/amb/run.py run --dataset longmemeval --split s \\
        --memory amplifier-memory --query-limit 20

Environment:
    AMB_PATH   Path to a local ``agent-memory-benchmark`` checkout, used
               only when ``memory_bench`` is not already importable (i.e.
               AMB was not installed into this interpreter). Its ``src/``
               directory is prepended to ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent


def _ensure_on_path() -> None:
    """Make ``benchmarks/amb/`` (this adapter) and AMB itself importable."""
    if str(_THIS_DIR) not in sys.path:
        sys.path.insert(0, str(_THIS_DIR))

    try:
        import memory_bench  # noqa: F401

        return
    except ImportError:
        pass

    import os

    amb_path = os.environ.get("AMB_PATH")
    if not amb_path:
        raise SystemExit(
            "agent-memory-benchmark ('memory_bench') is not installed in this "
            "interpreter and AMB_PATH is not set.\n"
            "Either install it as a dev dependency:\n"
            "  uv run --with git+https://github.com/vectorize-io/agent-memory-benchmark "
            "python benchmarks/amb/run.py ...\n"
            "or clone it and point AMB_PATH at the checkout root:\n"
            "  git clone https://github.com/vectorize-io/agent-memory-benchmark /tmp/amb\n"
            "  AMB_PATH=/tmp/amb python benchmarks/amb/run.py ...\n"
            "See benchmarks/amb/README.md."
        )
    src_dir = Path(amb_path).expanduser() / "src"
    if not src_dir.is_dir():
        raise SystemExit(
            f"AMB_PATH={amb_path!r} has no src/ directory -- expected the "
            "agent-memory-benchmark repo root (containing src/memory_bench)."
        )
    sys.path.insert(0, str(src_dir))


def main() -> None:
    _ensure_on_path()

    from amplifier_memory_provider import AmplifierMemoryProvider
    from memory_bench.memory import (
        REGISTRY,
    )  # the live dict; mutated in place, not replaced
    from memory_bench.memory.base import MemoryProvider

    # The adapter is written against AMB's shapes but does not import AMB at
    # module load (so the smoke test runs without it). Mix in the real ABC
    # here so the runner gets its concrete initialize()/async_retrieve()/...
    class _RegisteredProvider(AmplifierMemoryProvider, MemoryProvider):
        pass

    REGISTRY["amplifier-memory"] = _RegisteredProvider

    # AMB's CLI is a typer app that reads sys.argv itself; `from .cli import
    # REGISTRY as MEMORY_REGISTRY` there binds the SAME dict object we just
    # mutated above, so registration is visible without touching cli.py.
    from memory_bench.cli import app

    app()


if __name__ == "__main__":
    main()
