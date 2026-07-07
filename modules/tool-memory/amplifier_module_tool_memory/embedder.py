"""Local embedder for the memory daemon (D1, D2 of
docs/plans/2026-07-07-native-cutover-design.md).

``FastEmbedEmbedder`` wraps fastembed's ``TextEmbedding`` (ONNX Runtime, no
torch) behind the ``amplifier_data.embedding.Embedder`` protocol
(``embed(text) -> Sequence[float]``). It is used ONLY inside the memory
daemon (\u00a74.2): writes and queries share one resident model instance, which
is the same structural guarantee a server-side embedding always gave the
interject fix.

Lifecycle (\u00a74.3):

* Construction is cheap and never touches the network or loads the model.
* :meth:`warm` loads the model (first run downloads it from the HF Hub to a
  local cache) -- call it on a background thread so the daemon can start
  serving immediately (lazy, non-blocking warm-load).
* :attr:`ready` flips True once the model is loaded; :attr:`failed` carries a
  reason string when warm-load fails (no network, corrupted cache, fastembed
  not installed) -- the daemon stays up either way (loud-but-graceful, KG-N3).
* :meth:`embed` raises :class:`EmbedderUnavailable` if called before ready or
  after a failed warm-load -- callers (the daemon's dispatch tools) MUST check
  :attr:`ready` first and route to the lexical-only degraded path instead of
  letting this exception escape to a session.

``lexical_score`` is the deterministic pure-Python token-overlap term used to
re-rank vector search results (\u00a76.1) and to score candidates in the fully
degraded (embedder-unavailable) search path (\u00a76.2).
"""

from __future__ import annotations

import re
import threading

#: Pinned model: 384-dim, matching the legacy vendor store's embedding space, so
#: migrated vectors (D7, verbatim copy) and freshly embedded queries share one
#: vector space.
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

__all__ = ["DEFAULT_MODEL", "EmbedderUnavailable", "FastEmbedEmbedder", "lexical_score"]


class EmbedderUnavailable(RuntimeError):
    """Raised by :meth:`FastEmbedEmbedder.embed` when the model is not ready."""


class FastEmbedEmbedder:
    """``amplifier_data.embedding.Embedder`` implementation backed by fastembed.

    Thread-safe: :meth:`warm` may run on a background thread while other
    threads call :attr:`ready` / :attr:`failed` / :meth:`embed` concurrently
    (the daemon's HTTP handler threads). A single internal lock serializes
    the one-time model load; ``embed`` calls themselves are NOT serialized by
    this class (fastembed's ``TextEmbedding.embed`` is safe for concurrent use
    once loaded) -- the daemon's *write* lock, not this one, is what
    serializes mutating store operations.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._lock = threading.Lock()
        self._model: object | None = None
        self._ready = False
        self._failed: str | None = None

    def warm(self) -> None:
        """Load the model. Safe to call more than once (idempotent); never raises.

        Intended to run on a background thread from ``run_daemon`` so the
        daemon can start serving requests immediately. On any failure
        (fastembed not installed, no network for the first-time model
        download, a corrupted local cache, ...) records the reason in
        :attr:`failed` and leaves :attr:`ready` False -- the daemon degrades
        to lexical-only search rather than breaking (KG-N3).
        """
        with self._lock:
            if self._ready or self._failed is not None:
                return  # already warmed (or already gave up) -- idempotent
            try:
                from fastembed import TextEmbedding  # type: ignore[import-not-found]

                self._model = TextEmbedding(model_name=self.model_name)
                self._ready = True
            except (
                Exception
            ) as exc:  # pragma: no cover - exercised via forced-fail tests
                self._failed = f"{type(exc).__name__}: {exc}"

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def failed(self) -> str | None:
        return self._failed

    def embed(self, text: str) -> list[float]:
        """Embed *text*; raises :class:`EmbedderUnavailable` if not ready.

        Callers in the daemon dispatch layer check :attr:`ready` before
        calling this (and route to the lexical-only path when it is False),
        so this exception is a programming-error guard, not a normal control
        flow signal -- it should never surface to a session.
        """
        if not self._ready or self._model is None:
            reason = self._failed or "embedder not warmed yet"
            raise EmbedderUnavailable(reason)
        # fastembed's TextEmbedding.embed is a generator of numpy arrays.
        (vec,) = list(self._model.embed([text]))  # type: ignore[attr-defined]
        return [float(x) for x in vec]


# ---------------------------------------------------------------------------
# Lexical scoring (\u00a76.1, \u00a76.2) -- deterministic, stdlib-only, no model needed.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\w+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def lexical_score(query: str, text: str) -> float:
    """Deterministic pure-Python token-overlap score in [0, 1].

    ``|q \u2229 d| / max(1, |q|)`` over lowercased ``\\w+`` tokens -- fraction of
    the query's distinct tokens that also appear in *text*. Empty query or
    text yields 0.0 (never raises, never divides by zero).
    """
    if not query or not text:
        return 0.0
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    d_tokens = _tokenize(text)
    overlap = len(q_tokens & d_tokens)
    return overlap / max(1, len(q_tokens))
