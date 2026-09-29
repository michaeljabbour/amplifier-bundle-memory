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
    AMPLIFIER_AMB_QUERY_IDS
               Optional path to a file of query ids (one per line). When set,
               the dataset's queries are restricted to exactly those ids and
               its documents to those queries' isolation units (``user_id``)
               -- a fixed-subset run (T6.6) without editing AMB.
    AMPLIFIER_AMB_HYBRID_DEVICE
               Optional torch device (e.g. ``mps``) for AMB's own hybrid
               baseline provider's dense encoder, which AMB pins to ``cpu``.
               Speed only: the model, chunking and fusion are unchanged.
    AMPLIFIER_AMB_RETRY_DISCONNECTS
               ``on`` retries an answer/judge request that fails with a
               dropped connection (``RemoteProtocolError`` and similar), which
               AMB's own retry policy does not cover -- one such error would
               otherwise abort the run and discard the current unit. Retry
               policy only; prompts, models and scoring are unchanged.
    AMPLIFIER_AMB_REQUEST_TIMEOUT_S
               With the above on: per-request timeout in seconds. A request
               that exceeds it (a degenerate, never-ending generation) is
               raised as an unparseable answer -- the same failure AMB raises
               after its own retries, just without waiting ~15 min for it.
    AMPLIFIER_AMB_FALLBACK_MODEL
               With a timeout set: retry a timed-out request ONCE on this
               model (e.g. ``gemini-2.5-flash``) before failing; each use
               logs ``[fallback-model]`` so a run can count and strictly
               score those rows.
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


def _restrict_to_query_ids(cli_module, ids_path: str) -> None:
    """Wrap AMB's ``get_dataset`` (as bound in its CLI module) so the returned
    dataset only yields the listed query ids and their units' documents.
    Wraps the instance's bound methods; no AMB source is copied or edited."""
    wanted = {
        line.strip()
        for line in Path(ids_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    original_get = cli_module.get_dataset

    def get_dataset(name):
        ds = original_get(name)
        orig_queries = ds.load_queries
        orig_docs = ds.load_documents
        kept_users: set = set()

        def load_queries(*args, **kwargs):
            qs = [q for q in orig_queries(*args, **kwargs) if q.id in wanted]
            kept_users.update(q.user_id for q in qs)
            return qs

        def load_documents(*args, **kwargs):
            docs = orig_docs(*args, **kwargs)
            return [d for d in docs if d.user_id in kept_users]

        ds.load_queries = load_queries
        ds.load_documents = load_documents
        return ds

    cli_module.get_dataset = get_dataset


def _override_hybrid_device(device: str) -> None:
    """Rebind the dense-encoder class AMB's hybrid provider module uses so
    it loads on *device* instead of its hard-coded ``cpu``."""
    import memory_bench.memory.hybrid_search as hybrid

    original = hybrid.SentenceTransformer

    import threading

    lock = threading.Lock()  # GPU backends are not safe for AMB's concurrent queries

    def on_device(*args, **kwargs):
        kwargs["device"] = device
        model = original(*args, **kwargs)
        encode = model.encode

        def locked_encode(*a, **kw):
            with lock:
                return encode(*a, **kw)

        model.encode = locked_encode
        return model

    hybrid.SentenceTransformer = on_device


def _retry_disconnects(
    attempts: int = 5,
    base_delay: float = 10.0,
    timeout_s: float | None = None,
    fallback_model: str | None = None,
) -> None:
    """Wrap AMB's Gemini request method so dropped connections are retried
    (and, with *timeout_s*, over-long requests fail fast as unparseable)."""
    import logging
    import time

    import memory_bench.llm.gemini as gemini

    log = logging.getLogger(__name__)
    if timeout_s:
        from google import genai
        from google.genai import types

        original_init = gemini.GeminiLLM.__init__

        def __init__(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self._client = genai.Client(
                http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
            )
            models = self._client.models
            generate = models.generate_content

            def generate_content(*a, **kw):
                # Our deadline surfaces as a server-side DEADLINE_EXCEEDED,
                # which AMB would retry (6x, with backoff). Fail fast instead,
                # worded so neither AMB's retry filters nor ours match it.
                try:
                    return generate(*a, **kw)
                except Exception as exc:
                    if "DEADLINE_EXCEEDED" not in str(exc):
                        raise
                if fallback_model:
                    # One inline retry on the fallback model (the protocol the
                    # baseline applied by hand); logged so runs can count it
                    # and score those rows strictly as wrong.
                    log.warning("[fallback-model] %s after timeout", fallback_model)
                    try:
                        return generate(*a, **{**kw, "model": fallback_model})
                    except Exception as exc:
                        if "DEADLINE_EXCEEDED" not in str(exc):
                            raise
                raise RuntimeError(
                    "Gemini returned unparseable response "
                    f"(request exceeded {timeout_s:.0f} s)"
                )

            models.generate_content = generate_content

        gemini.GeminiLLM.__init__ = __init__

    original = gemini.GeminiLLM._generate_raw

    def _generate_raw(self, *args, **kwargs):
        delay = base_delay
        for attempt in range(attempts):
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:  # httpx transport errors, by name
                name = type(exc).__name__
                if "Timeout" in name or "timed out" in str(exc).lower():
                    raise RuntimeError(
                        "Gemini returned unparseable response "
                        f"(request exceeded {timeout_s} s: {name})"
                    ) from exc
                transient = name in (
                    "RemoteProtocolError",
                    "ReadError",
                    "ConnectError",
                    "WriteError",
                )
                if not transient or attempt == attempts - 1:
                    raise
                log.warning(
                    "[retry-disconnect] %s, retry %d in %.0fs", name, attempt + 1, delay
                )
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    gemini.GeminiLLM._generate_raw = _generate_raw


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
    import os

    import memory_bench.cli as amb_cli

    if os.environ.get("AMPLIFIER_AMB_RETRY_DISCONNECTS", "off").lower() == "on":
        timeout = os.environ.get("AMPLIFIER_AMB_REQUEST_TIMEOUT_S")
        _retry_disconnects(
            timeout_s=float(timeout) if timeout else None,
            fallback_model=os.environ.get("AMPLIFIER_AMB_FALLBACK_MODEL") or None,
        )

    device = os.environ.get("AMPLIFIER_AMB_HYBRID_DEVICE")
    if device:
        _override_hybrid_device(device)

    ids_path = os.environ.get("AMPLIFIER_AMB_QUERY_IDS")
    if ids_path:
        _restrict_to_query_ids(amb_cli, ids_path)

    amb_cli.app()


if __name__ == "__main__":
    main()
