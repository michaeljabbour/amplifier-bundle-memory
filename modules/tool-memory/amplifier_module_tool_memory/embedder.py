"""Local embedder for the memory daemon (D1, D2 of
the native-cutover design history).

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

#: Alternative 384-dim retrieval model (T6.5, D29): same vector dimension as
#: :data:`DEFAULT_MODEL` (verified via ``TextEmbedding.get_embedding_size``
#: against fastembed 0.8.0), so switching to it does not require a schema
#: change -- only a re-embed sweep (old and new vectors are NOT
#: interchangeable, just dimension-compatible; ``store.py``'s
#: mixed-dimension bailout guards against comparing across a sweep in
#: flight). Verified present via
#: ``TextEmbedding.list_supported_models()``.
BGE_SMALL_MODEL = "BAAI/bge-small-en-v1.5"

#: model name -> vector dimension, for models this module has verified against
#: the installed fastembed's registry. Used to populate :attr:`dim` /
#: :attr:`model_id` without loading the model (a registry lookup, not a
#: network call). Models outside this map still work -- :attr:`dim` falls
#: back to a live registry lookup at construction, and to ``None`` if the
#: model is unknown to fastembed's registry too (a custom/local model).
_KNOWN_MODEL_DIMS: dict[str, int] = {
    DEFAULT_MODEL: 384,
    BGE_SMALL_MODEL: 384,
}

__all__ = [
    "BGE_SMALL_MODEL",
    "DEFAULT_MODEL",
    "EmbedderUnavailable",
    "FastEmbedEmbedder",
    "lexical_score",
]


def _resolve_dim(model_name: str) -> int | None:
    """Best-effort vector dimension for *model_name*, or ``None``.

    Checks the local map first (no import needed for the two models this
    module knows about), then falls back to fastembed's own registry lookup
    (``TextEmbedding.get_embedding_size`` -- a static lookup against
    fastembed's bundled model metadata, not a network call or model load).
    Returns ``None`` if fastembed is not installed or the model is unknown
    to its registry (e.g. a custom/local model) -- callers must not assume
    :attr:`FastEmbedEmbedder.dim` is always an ``int``.
    """
    if model_name in _KNOWN_MODEL_DIMS:
        return _KNOWN_MODEL_DIMS[model_name]
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        return int(TextEmbedding.get_embedding_size(model_name))
    except Exception:  # noqa: BLE001 - unknown/custom model or fastembed missing
        return None


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
        #: Vector dimension for *model_name*, resolved at construction time
        #: from a static registry lookup (never triggers a model load or
        #: network call). ``None`` for a model unknown to both this module's
        #: map and fastembed's own registry (a custom/local model) -- the
        #: real dimension is then only knowable after :meth:`warm` succeeds.
        self.dim: int | None = _resolve_dim(model_name)
        #: Stable identity string for telemetry/config comparisons (T6.5):
        #: model name plus resolved dimension, e.g.
        #: ``"BAAI/bge-small-en-v1.5:384"``. Falls back to the bare model
        #: name when :attr:`dim` could not be resolved.
        self.model_id = (
            f"{model_name}:{self.dim}" if self.dim is not None else model_name
        )
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
            except Exception as exc:  # noqa: BLE001 - pragma: no cover, forced-fail tests
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

    def embed_query(self, text: str) -> list[float]:
        """Embed a *query* string (T6.5): asymmetric-retrieval aware.

        Some embedding models (bge-style) benefit from a query-specific
        instruction prefix distinct from how documents are embedded --
        fastembed exposes this as ``TextEmbedding.query_embed`` when the
        underlying model implementation provides one. This method prefers
        that path and falls back to plain ``embed`` for models (like
        :data:`DEFAULT_MODEL`) that have no query-specific implementation,
        so results are identical to :meth:`embed` for the current default
        model and only diverge where a model actually defines the
        asymmetry. Raises :class:`EmbedderUnavailable` under the same
        conditions as :meth:`embed`.
        """
        if not self._ready or self._model is None:
            reason = self._failed or "embedder not warmed yet"
            raise EmbedderUnavailable(reason)
        query_embed = getattr(self._model, "query_embed", None)
        if query_embed is None:
            return self.embed(text)
        (vec,) = list(query_embed([text]))
        return [float(x) for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of *documents/passages* (T6.5): asymmetric-retrieval aware.

        Mirrors :meth:`embed_query` on the document/passage side --
        prefers fastembed's ``TextEmbedding.passage_embed`` when the
        underlying model provides one, else falls back to plain ``embed``
        per text (identical output to calling :meth:`embed` once per text
        for models with no passage-specific implementation). Raises
        :class:`EmbedderUnavailable` under the same conditions as
        :meth:`embed`.
        """
        if not self._ready or self._model is None:
            reason = self._failed or "embedder not warmed yet"
            raise EmbedderUnavailable(reason)
        if not texts:
            return []
        passage_embed = getattr(self._model, "passage_embed", None)
        if passage_embed is None:
            return [self.embed(t) for t in texts]
        vecs = list(passage_embed(texts))
        return [[float(x) for x in vec] for vec in vecs]


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
