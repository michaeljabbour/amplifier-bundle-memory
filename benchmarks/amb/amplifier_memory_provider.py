"""Agent Memory Benchmark (AMB) adapter for the Amplifier native memory store.

T0.4 of docs/plans/2026-09-27-memory-layers-design.md.

AMB (github.com/vectorize-io/agent-memory-benchmark) ships NO LICENSE file,
so nothing from it is vendored here. This module only *subclasses* its
``MemoryProvider`` ABC at runtime, against an externally installed/cloned
copy of AMB (see README.md for install instructions) -- no AMB source is
copied into this repository.

Verified interface (2026-09-27, against AMB HEAD cloned to a scratch dir):
    - ``memory_bench.memory.base.MemoryProvider``: class attrs ``name``,
      ``description``, ``kind``, ``concurrency``, ``supports_filters``;
      optional ``initialize()``, ``cleanup()``,
      ``prepare(store_dir: Path, unit_ids: set[str] | None, reset: bool = True)``;
      abstract ``ingest(documents: list[Document]) -> None`` and
      ``retrieve(query, k=10, user_id=None, query_timestamp=None,
      filters=None) -> tuple[list[Document], dict | None]``.
    - ``memory_bench.models.Document``: ``id``, ``content``, ``user_id``,
      ``messages``, ``timestamp``, ``context``, ``source_ids``, ``tags``.
    - ``memory_bench.memory.REGISTRY``: ``dict[str, type[MemoryProvider]]``,
      instantiated with no constructor arguments by
      ``get_memory_provider()``. Configuration for a registry-mediated
      instantiation therefore has to travel through environment variables
      (``AMPLIFIER_AMB_EMBEDDER``, ``AMPLIFIER_AMB_FUSION``,
      ``AMPLIFIER_AMB_LAYERS``) -- direct
      construction (as the smoke test does) can pass constructor kwargs
      instead.

Deviation from the task brief: the real ``retrieve`` signature also accepts
an optional ``filters`` dict (used only when a provider sets
``supports_filters = True``); this provider does not set that flag, so
``filters`` is accepted and ignored, matching every other non-filtering
provider in AMB's own registry.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["AmplifierMemoryProvider"]

#: Reused across ingest/retrieve so a query's tokens can be compared with a
#: room name the same way :meth:`_room_for` derives it from tags.
_SAFE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"


def _sanitize(label: str) -> str:
    """Lowercase, ``_``-joined room/wing label -- never empty."""
    cleaned = "".join(c if c.lower() in _SAFE_CHARS else "_" for c in label.lower())
    cleaned = cleaned.strip("_") or "default"
    return cleaned


class AmplifierMemoryProvider:
    """AMB ``MemoryProvider`` backed by ``NativeMemoryStore``.

    Each AMB ``user_id`` maps to its own wing (``amb_{user_id}``) so
    per-user isolation is structural (amplifier-data scope edges), not a
    filter applied after the fact. Each document's first tag (or
    ``"default"``) becomes its room, so a scoped retrieval can narrow
    further than "everything this user ever said" when the dataset
    supplies tags.

    Never touches ``~/.amplifier/memory``: the backing store always lives
    under an isolated, throwaway home -- either the ``store_dir`` AMB's own
    runner hands to :meth:`prepare` (``outputs/<dataset>/<provider>/_store/
    ...``), or, for direct construction outside the AMB runner (the smoke
    test), a fresh ``tempfile.mkdtemp`` directory.
    """

    name = "amplifier-memory"
    description = (
        "Amplifier's three-layer NativeMemoryStore (amplifier-data substrate), "
        "used directly (no daemon) with per-user wing scoping."
    )
    kind = "local"
    provider = "amplifier"
    variant = "native"
    link = "https://github.com/microsoft/amplifier"
    concurrency = 1  # AmplifierStore's direct backend is not verified thread-safe.
    supports_filters = False

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        embedder: str | None = None,
        fusion: str | None = None,
        layers: str | Sequence[str] | None = None,
    ) -> None:
        """``embedder``: ``"auto"`` (try FastEmbedEmbedder, degrade to lexical-only
        on any failure -- the default) or ``"none"`` (always lexical-only, never
        even attempts to load a model; use this for offline/CI runs). Falls back
        to the ``AMPLIFIER_AMB_EMBEDDER`` env var when not given explicitly,
        which is the only configuration channel available when AMB's own
        registry instantiates this class with no arguments.
        """
        self._home: Path | None = Path(home).expanduser() if home is not None else None
        self._embedder_mode = embedder or os.environ.get(
            "AMPLIFIER_AMB_EMBEDDER", "auto"
        )
        if self._embedder_mode not in ("auto", "none"):
            raise ValueError(
                f"embedder must be 'auto' or 'none', got {self._embedder_mode!r}"
            )
        # ``fusion``: ``"rrf"`` (default) or ``"legacy"`` -- passed straight to
        # ``NativeMemoryStore.search(fusion=...)``. ``layers``: comma-separated
        # cell kinds (default ``"drawer"``; the benchmark only ingests drawers).
        # Env fallbacks: ``AMPLIFIER_AMB_FUSION`` / ``AMPLIFIER_AMB_LAYERS``.
        self._fusion = fusion or os.environ.get("AMPLIFIER_AMB_FUSION", "rrf")
        if self._fusion not in ("rrf", "legacy"):
            raise ValueError(f"fusion must be 'rrf' or 'legacy', got {self._fusion!r}")
        raw_layers = layers or os.environ.get("AMPLIFIER_AMB_LAYERS", "drawer")
        if isinstance(raw_layers, str):
            raw_layers = raw_layers.split(",")
        self._layers = tuple(x.strip() for x in raw_layers if x.strip())
        if not self._layers or not set(self._layers) <= {"drawer", "fact"}:
            raise ValueError(f"layers must be drawer and/or fact, got {raw_layers!r}")
        self.last_raw: dict[str, Any] | None = None
        self._store: Any = None
        self._embedder: Any = None
        self._embedder_load_attempted = False

    # ------------------------------------------------------------------
    # MemoryProvider optional lifecycle hooks
    # ------------------------------------------------------------------

    def prepare(
        self,
        store_dir: Path,
        unit_ids: set[str] | None = None,
        reset: bool = True,
    ) -> None:
        """Point the store at AMB's own per-run storage directory.

        ``reset=True`` (AMB's default whenever ingestion runs) wipes any
        prior contents first, so re-running a benchmark never accumulates
        stale drawers from an earlier attempt at the same split.
        """
        store_dir = Path(store_dir)
        if reset and store_dir.exists():
            shutil.rmtree(store_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        self._home = store_dir
        self._close_store()

    def cleanup(self) -> None:
        self._close_store()

    def _close_store(self) -> None:
        if self._store is not None:
            close = getattr(self._store, "close", None)
            if callable(close):
                close()
        self._store = None

    # ------------------------------------------------------------------
    # Store / embedder construction (lazy -- works with or without prepare())
    # ------------------------------------------------------------------

    def _ensure_store(self) -> Any:
        if self._store is None:
            from amplifier_module_tool_memory.store import NativeMemoryStore

            home = self._home
            if home is None:
                home = Path(tempfile.mkdtemp(prefix="amb_amplifier_"))
                self._home = home
            home.mkdir(parents=True, exist_ok=True)
            # The store path is a log FILE; ``home`` is a directory (AMB's
            # prepare() hands one over), so the log lives inside it.
            self._store = NativeMemoryStore(path=str(home / "memory.log"))
        return self._store

    def _get_embedder(self) -> Any | None:
        if self._embedder_mode == "none":
            return None
        if not self._embedder_load_attempted:
            self._embedder_load_attempted = True
            from amplifier_module_tool_memory.embedder import FastEmbedEmbedder

            embedder = FastEmbedEmbedder()
            embedder.warm()  # never raises; sets .ready/.failed instead
            self._embedder = embedder if embedder.ready else None
        return self._embedder

    def _embed(self, text: str) -> Sequence[float] | None:
        embedder = self._get_embedder()
        if embedder is None:
            return None
        return embedder.embed(text)

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wing_for(user_id: str | None) -> str:
        return f"amb_{_sanitize(user_id)}" if user_id else "amb_default"

    @staticmethod
    def _room_for(tags: list[str] | None) -> str:
        if tags:
            return _sanitize(tags[0])
        return "default"

    # ------------------------------------------------------------------
    # MemoryProvider contract
    # ------------------------------------------------------------------

    def ingest(self, documents: list[Any]) -> None:
        store = self._ensure_store()
        file_params = inspect.signature(store.file).parameters
        supports_filed_at = "filed_at" in file_params

        for doc in documents:
            wing = self._wing_for(doc.user_id)
            room = self._room_for(doc.tags)
            embedding = self._embed(doc.content)

            kwargs: dict[str, Any] = {
                "wing": wing,
                "room": room,
                "content": doc.content,
                # AMB's own doc id is the ONE thing retrieve() must be able
                # to hand back -- carried verbatim as this drawer's source,
                # a currently-valid queryable KG fact (has_source).
                "source": doc.id,
                "embedding": embedding,
            }
            if supports_filed_at and doc.timestamp:
                kwargs["filed_at"] = doc.timestamp
            store.file(**kwargs)

    def retrieve(
        self,
        query: str,
        k: int = 10,
        user_id: str | None = None,
        query_timestamp: str | None = None,
        filters: dict | None = None,
    ) -> tuple[list[Any], dict | None]:
        from memory_bench.models import Document

        store = self._ensure_store()
        wing = self._wing_for(user_id)
        query_vector = self._embed(query)

        hits = store.search(
            query_vector,
            k,
            wing=wing,
            lexical_query=query,
            fusion=self._fusion,
            layers=self._layers,
        )

        documents: list[Any] = []
        scores: dict[str, float] = {}
        for hit in hits:
            doc_id = hit.get("source") or str(hit["ref"])
            documents.append(
                Document(
                    id=doc_id,
                    content=hit["content"],
                    user_id=user_id,
                    tags=[hit["room"]] if hit.get("room") else None,
                    source_ids=[doc_id],
                )
            )
            scores[doc_id] = hit["score"]

        raw: dict[str, Any] = {
            "degraded": query_vector is None,
            "fusion": self._fusion,
            "scores": scores,
            # Per-hit RRF arm ranks (None under fusion="legacy") and layer.
            "arms": {
                hit.get("source") or str(hit["ref"]): hit.get("arms") for hit in hits
            },
            "layer": {
                hit.get("source") or str(hit["ref"]): hit.get("layer") for hit in hits
            },
        }
        trace = os.environ.get("AMPLIFIER_AMB_TRACE")
        if trace:
            # AMB drops raw_response from saved results, so R@k against
            # gold_ids is only recoverable from this per-query id trace.
            with open(trace, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "user_id": user_id,
                            "query": query,
                            "k": k,
                            "fusion": self._fusion,
                            "ids": [d.id for d in documents],
                        }
                    )
                    + "\n"
                )
        # Diagnostics stay OFF the returned raw_response: several AMB dataset
        # prompts (longmemeval, locomo, lifebench) substitute
        # ``json.dumps(raw_response)`` for the rendered context whenever it is
        # non-empty, so a scores-only dict would hide every retrieved document
        # from the answer model. Returning None makes AMB render its standard
        # "## Memory i" context -- the same text its context_tokens counts.
        self.last_raw = raw
        return documents, None
