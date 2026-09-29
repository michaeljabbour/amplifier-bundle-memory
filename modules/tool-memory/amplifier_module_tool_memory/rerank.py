"""Optional local cross-encoder reranker for the memory store (T6.4, D29).

Same runtime posture as :mod:`embedder`: fastembed's ONNX-backed cross-encoder,
no torch, no network beyond fastembed's own first-run model download. This
module is deliberately *store-agnostic* -- ``store.py`` calls
:func:`get_reranker` with its config dict and gets back either a
:class:`Reranker` or ``None`` (rerank disabled or unavailable), then calls
``.score(query, texts)`` on the fused top-N candidates. It never imports
``store`` or ``daemon`` (one-way dependency, per the module boundary this
builder owns).

Lifecycle mirrors the embedder's loud-but-graceful posture (KG-N3): a missing
``fastembed`` install, a missing ``rerank`` extra, or a failed model load
never raises out of :func:`get_reranker` -- it returns ``None`` and the caller
falls back to the fused (non-reranked) ranking.
"""

from __future__ import annotations

import threading
from typing import Protocol

#: Default reranker: the smallest MiniLM-L-6 MS-MARCO cross-encoder fastembed
#: ships (~80MB, verified via ``TextCrossEncoder.list_supported_models()``
#: against fastembed 0.8.0 -- the only "MiniLM-L-6" + "ms-marco" combination
#: fastembed's registry offers, and the smallest reranker in the registry
#: overall). 384-dim query/passage encoders elsewhere in this module are
#: unrelated -- a cross-encoder scores (query, text) pairs directly, it has
#: no separate embedding dimension.
DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

#: Truncate each candidate text to this many characters before scoring
#: (config key ``rerank_max_chars``). Keeps batches small and bounds latency;
#: cross-encoders attend over the full (query, text) pair so long texts cost
#: roughly linearly.
DEFAULT_RERANK_MAX_CHARS = 1500

#: Config key ``rerank`` values. "off" (default) disables reranking entirely;
#: any other value is treated as "on" only if it's a recognized mode below.
_RERANK_MODES = {"off", "cross-encoder"}

__all__ = [
    "DEFAULT_RERANK_MAX_CHARS",
    "DEFAULT_RERANK_MODEL",
    "CrossEncoderReranker",
    "Reranker",
    "get_reranker",
]


class Reranker(Protocol):
    """Contract the store calls exactly this shape against.

    ``model_id`` is a stable string (model name) so callers/telemetry can
    record which reranker scored a given result set without importing this
    module's internals.
    """

    model_id: str

    def score(self, query: str, texts: list[str]) -> list[float]:
        """Return one relevance score per text, same order as *texts*.

        Higher is more relevant. Never raises for well-formed input (empty
        *texts* returns ``[]``); truncates each text to the configured
        ``rerank_max_chars`` before scoring.
        """
        ...


class CrossEncoderReranker:
    """``Reranker`` backed by fastembed's ``TextCrossEncoder``.

    Lazy-loads the ONNX model on first :meth:`score` call (construction is
    cheap and never touches the network or loads the model -- same posture as
    :class:`amplifier_module_tool_memory.embedder.FastEmbedEmbedder`). A
    process-wide cache (:func:`get_reranker`) keys singleton instances by
    model name, so repeated ``get_reranker`` calls with the same config share
    one loaded model instead of re-loading per call.

    Thread-safe: a lock serializes the one-time model load; ``score`` calls
    themselves are NOT serialized by this class beyond that (fastembed's
    ``TextCrossEncoder.rerank`` is safe for concurrent use once loaded).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        max_chars: int = DEFAULT_RERANK_MAX_CHARS,
    ) -> None:
        self.model_id = model_name
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._model: object | None = None
        self._unavailable = False

    def _ensure_loaded(self) -> object | None:
        """Load the model on first use; returns ``None`` if unavailable.

        Never raises -- a missing ``fastembed`` install, a missing
        ``rerank`` extra (``fastembed.rerank`` not importable), or a failed
        model load (no network for the first-run download, corrupted cache)
        all land here and are recorded as permanently unavailable for this
        instance (loud-but-graceful, KG-N3).
        """
        if self._model is not None:
            return self._model
        if self._unavailable:
            return None
        with self._lock:
            if self._model is not None:
                return self._model
            if self._unavailable:
                return None
            try:
                from fastembed.rerank.cross_encoder import (  # type: ignore[import-not-found]
                    TextCrossEncoder,
                )

                self._model = TextCrossEncoder(model_name=self.model_id)
            except Exception:  # noqa: BLE001 - pragma: no cover, forced-fail tests
                self._unavailable = True
                return None
            return self._model

    def score(self, query: str, texts: list[str]) -> list[float]:
        """Score *texts* against *query*; see :class:`Reranker`.

        Returns ``[]`` immediately for an empty *texts* list (no model load
        needed). If the model is unavailable (see :meth:`_ensure_loaded`),
        returns a neutral score of ``0.0`` for every text rather than raising
        -- callers should treat this as "reranking had no effect" and keep
        the pre-rerank order, the same way the embedder's callers check
        ``.ready`` before trusting a vector.
        """
        if not texts:
            return []
        model = self._ensure_loaded()
        if model is None:
            return [0.0] * len(texts)
        truncated = [t[: self.max_chars] for t in texts]
        scores = list(model.rerank(query, truncated))  # type: ignore[attr-defined]
        return [float(s) for s in scores]


# ---------------------------------------------------------------------------
# Process-wide singleton cache (§ "cache a process-wide singleton per model")
# ---------------------------------------------------------------------------

_INSTANCES: dict[tuple[str, int], CrossEncoderReranker] = {}
_INSTANCES_LOCK = threading.Lock()


def _get_or_create(model_name: str, max_chars: int) -> CrossEncoderReranker:
    key = (model_name, max_chars)
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(key)
        if instance is None:
            instance = CrossEncoderReranker(model_name=model_name, max_chars=max_chars)
            _INSTANCES[key] = instance
        return instance


def get_reranker(config: dict) -> Reranker | None:
    """Build (or fetch a cached) reranker from *config*, or ``None``.

    ``config`` keys:

    * ``rerank``: ``"off"`` (default) or ``"cross-encoder"``. Any other
      value (typo, future mode not yet implemented) is treated as ``"off"``
      -- unrecognized config degrades to no-rerank rather than raising.
    * ``rerank_model``: model name, default :data:`DEFAULT_RERANK_MODEL`.
    * ``rerank_max_chars``: default :data:`DEFAULT_RERANK_MAX_CHARS`.

    Returns ``None`` when ``rerank`` is ``"off"`` or unrecognized. Does NOT
    return ``None`` just because fastembed might be unavailable -- that
    failure is deferred to first :meth:`Reranker.score` call (lazy load), at
    which point the returned reranker degrades to neutral scores rather than
    raising. This keeps ``get_reranker`` itself synchronous and side-effect
    free (never touches the network, never imports fastembed).
    """
    mode = config.get("rerank", "off")
    if mode not in _RERANK_MODES or mode == "off":
        return None
    model_name = config.get("rerank_model", DEFAULT_RERANK_MODEL)
    max_chars = config.get("rerank_max_chars", DEFAULT_RERANK_MAX_CHARS)
    return _get_or_create(model_name, max_chars)
