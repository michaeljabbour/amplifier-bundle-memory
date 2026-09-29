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
        granularity: str | None = None,
        rerank: bool | str | None = None,
        dates: bool | str | None = None,
        embedding_model: str | None = None,
        passages: bool | str | None = None,
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
        # T6.3: "granularity" ("passage" default or "drawer") and "rerank"
        # (bool, default off) are passed to store.search ONLY when the
        # store's own signature accepts them (inspect.signature guard in
        # retrieve() -- the pinned substrate at HEAD does not yet, so this
        # stays a no-op until that lands). "dates" (default on) toggles the
        # date-stamp prefix this adapter adds to returned Document content.
        # Env fallbacks: AMPLIFIER_AMB_GRANULARITY / AMPLIFIER_AMB_RERANK /
        # AMPLIFIER_AMB_DATES.
        self._granularity = granularity or os.environ.get(
            "AMPLIFIER_AMB_GRANULARITY", "passage"
        )
        if self._granularity not in ("passage", "drawer"):
            raise ValueError(
                f"granularity must be 'passage' or 'drawer', got {self._granularity!r}"
            )
        self._rerank = self._parse_bool_env(
            rerank, "AMPLIFIER_AMB_RERANK", default="off"
        )
        self._dates = self._parse_bool_env(dates, "AMPLIFIER_AMB_DATES", default="on")
        # T6.6: embedding model for the local embedder (empty/None -> the
        # embedder's own default). Env fallback: AMPLIFIER_AMB_EMBEDDING_MODEL.
        self._embedding_model = (
            embedding_model or os.environ.get("AMPLIFIER_AMB_EMBEDDING_MODEL") or None
        )
        # T6.6: "passages" (default on) -- off builds the store with passage
        # splitting disabled, reproducing the pre-T6.2 whole-drawer index
        # (the v2.0.1 / D26 retrieval proxy). Env: AMPLIFIER_AMB_PASSAGES.
        self._passages = self._parse_bool_env(
            passages, "AMPLIFIER_AMB_PASSAGES", default="on"
        )
        self.last_raw: dict[str, Any] | None = None
        self._store: Any = None
        self._embedder: Any = None
        self._embedder_load_attempted = False
        # T6.3: parent drawer ref -> AMB's own doc id, populated by ingest().
        # Lets a passage hit (whose "ref" is the PASSAGE cell, not the
        # drawer) map back to the original doc id via its "drawer_ref".
        self._drawer_to_doc_id: dict[str, str] = {}

    @staticmethod
    def _parse_bool_env(
        value: bool | str | None, env_name: str, *, default: str
    ) -> bool:
        """``value`` (constructor kwarg) wins; else read ``env_name``; else
        ``default``. Accepts a real bool, or the strings on/off (also
        true/false, 1/0 for convenience)."""
        if isinstance(value, bool):
            return value
        raw = value if value is not None else os.environ.get(env_name, default)
        raw = str(raw).strip().lower()
        if raw in ("on", "true", "1", "yes"):
            return True
        if raw in ("off", "false", "0", "no"):
            return False
        raise ValueError(f"{env_name} must be 'on' or 'off', got {raw!r}")

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
        self._drawer_to_doc_id = {}

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
            store_kwargs: dict[str, Any] = {"path": str(home / "memory.log")}
            if not self._passages and (
                "passage_min_chars"
                in inspect.signature(NativeMemoryStore.__init__).parameters
            ):
                # No drawer is ever long enough to split: no passage cells.
                store_kwargs["passage_min_chars"] = 1 << 60
            self._store = NativeMemoryStore(**store_kwargs)
            # T6.6: AMPLIFIER_AMB_FOLD_RETENTION_S overrides how long the
            # store keeps its folded view (and incremental-extension base)
            # after the last build. The product default (5 s) is shorter
            # than one AMB unit's ingest, so every query after an ingest
            # re-folds the WHOLE log -- O(store) latency. Latency-only knob:
            # ranking is unaffected. Unset -> product default.
            retention = os.environ.get("AMPLIFIER_AMB_FOLD_RETENTION_S")
            if retention and hasattr(self._store, "SNAPSHOT_REUSE_S"):
                self._store.SNAPSHOT_REUSE_S = float(retention)
        return self._store

    def _get_embedder(self) -> Any | None:
        if self._embedder_mode == "none":
            return None
        if not self._embedder_load_attempted:
            self._embedder_load_attempted = True
            from amplifier_module_tool_memory.embedder import FastEmbedEmbedder

            embedder = (
                FastEmbedEmbedder(self._embedding_model)
                if self._embedding_model
                else FastEmbedEmbedder()
            )
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
        # Mirror the daemon's `remember` path (T6.2/T6.5): record which model
        # produced the drawer vector, and hand the embedder over so passages
        # created for long content are embedded on write, not left lexical-only.
        supports_model_id = "embedding_model_id" in file_params
        supports_embedder = "embedder" in file_params
        embedder = self._get_embedder()
        model_id = getattr(embedder, "model_id", None) if embedder else None

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
            if supports_model_id and embedding is not None and model_id:
                kwargs["embedding_model_id"] = model_id
            if supports_embedder and embedder is not None:
                kwargs["embedder"] = embedder
            ref = store.file(**kwargs)
            # T6.3: remember which drawer this doc became, so a later
            # passage hit (whose own "ref" is the passage cell, not the
            # drawer) can map back to AMB's doc id via "drawer_ref".
            if ref is not None:
                self._drawer_to_doc_id[str(ref)] = doc.id

    @staticmethod
    def _date_stamp(hit: dict[str, Any]) -> str:
        """Return YYYY-MM-DD from observed_at (preferred) or filed_at; else empty."""
        for key in ("observed_at", "filed_at"):
            val = hit.get(key)
            if val:
                s = str(val)
                if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                    return s[:10]
        return ""

    def _render_for_amb(self, hit: dict[str, Any]) -> str:
        """Date-stamped hit content for a returned Document -- no provenance
        tag (that's a briefing/interject-only affordance; the benchmark's
        own gold_ids/answer-model comparisons should see plain dated text)."""
        content = (hit.get("content") or "").strip()
        if not self._dates:
            return content
        stamp = self._date_stamp(hit)
        return f"[{stamp}] {content}" if stamp else content

    def _doc_id_for_hit(self, hit: dict[str, Any]) -> str:
        """AMB's own doc id for one search hit.

        A passage hit's own ``ref`` addresses the PASSAGE cell, not the
        parent drawer -- resolve through ``drawer_ref`` (populated at
        ingest()) so gold_ids recall still keys on the original doc id.
        Falls back to the pre-T6.2 behaviour (source, else ref) when the
        hit isn't a passage or its parent wasn't seen by this provider.
        """
        if hit.get("layer") == "passage":
            drawer_ref = hit.get("drawer_ref")
            if drawer_ref is not None:
                mapped = self._drawer_to_doc_id.get(str(drawer_ref))
                if mapped is not None:
                    return mapped
        return hit.get("source") or str(hit.get("ref"))

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

        search_kwargs: dict[str, Any] = {
            "wing": wing,
            "lexical_query": query,
            "fusion": self._fusion,
            "layers": self._layers,
        }
        # T6.3: only pass the new params when the store's own signature
        # accepts them (inspect.signature guard) -- the pinned substrate may
        # not have T6.1's granularity/rerank support yet; degrading silently
        # keeps this adapter working against either version.
        search_params = inspect.signature(store.search).parameters
        if "granularity" in search_params:
            search_kwargs["granularity"] = self._granularity
        if "rerank" in search_params:
            search_kwargs["rerank"] = self._rerank
        embedder = self._get_embedder()
        if "current_model_id" in search_params and embedder is not None:
            search_kwargs["current_model_id"] = getattr(embedder, "model_id", None)

        hits = store.search(query_vector, k, **search_kwargs)

        # T6.2/T6.3: group hits by the ORIGINAL AMB doc id; multiple passage
        # hits of the same parent drawer are concatenated, in span order,
        # into ONE Document -- gold_ids recall keys on AMB's own doc id, not
        # on however many passages a long drawer was split into.
        order: list[str] = []
        grouped: dict[str, dict[str, Any]] = {}
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            doc_id = self._doc_id_for_hit(hit)
            group = grouped.get(doc_id)
            if group is None:
                group = {"first_hit": hit, "passages": []}
                grouped[doc_id] = group
                order.append(doc_id)
            span = hit.get("span")
            start = span[0] if isinstance(span, (list, tuple)) and span else 0
            group["passages"].append((start, hit))

        documents: list[Any] = []
        scores: dict[str, float] = {}
        arms: dict[str, Any] = {}
        layer: dict[str, Any] = {}
        for doc_id in order:
            group = grouped[doc_id]
            ordered_hits = [h for _, h in sorted(group["passages"], key=lambda p: p[0])]
            content = "\n\u2026\n".join(self._render_for_amb(h) for h in ordered_hits)
            first_hit = group["first_hit"]
            documents.append(
                Document(
                    id=doc_id,
                    content=content,
                    user_id=user_id,
                    tags=[first_hit["room"]] if first_hit.get("room") else None,
                    source_ids=[doc_id],
                )
            )
            scores[doc_id] = first_hit.get("score", 0.0)
            arms[doc_id] = first_hit.get("arms")
            layer[doc_id] = first_hit.get("layer")

        raw: dict[str, Any] = {
            "degraded": query_vector is None,
            "fusion": self._fusion,
            "granularity": self._granularity,
            "rerank": self._rerank,
            "scores": scores,
            # Per-doc RRF arm ranks (None under fusion="legacy") and layer.
            "arms": arms,
            "layer": layer,
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
                            "granularity": self._granularity,
                            "rerank": self._rerank,
                            "embedding_model": getattr(
                                self._embedder, "model_id", None
                            ),
                            "passages": self._passages,
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
