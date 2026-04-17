"""
Palace Garden — on-demand deep analysis of palace contents.

Spec: docs/spec-v1.2.0-gene-transfer.md Section 6.

Public API (used by PalaceTool.execute):
    execute_garden(mcp_fn, emit_fn, wing, room, lookback_days, max_drawers,
                   cluster_threshold, session_id) -> dict

Pure-function helpers (importable for tests, no MCP calls):
    find_clusters(adjacency, min_size) -> list[set[str]]
    cluster_id(member_ids)             -> str  (12-char hex)
    classify_cluster(member_ids, categories, texts) -> (label, dominant_category)
    extract_common_terms(texts, top_n) -> list[str]

Design notes:
- No chromadb dep — clustering uses mempalace_check_duplicate MCP calls.
- All heavy lifting (BFS, term frequency, cluster hash) is pure Python.
- Budget: max_drawers clamped to [1, 500]. Progress events every 50 drawers.
- Phase 3 rubric imported from .phase3 for importance backfill (Step 8).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .phase3 import compute_importance

# ── Constants ────────────────────────────────────────────────────────────────

_MAX_DRAWERS_HARD_CAP = 500
_DEFAULT_MAX_DRAWERS = 200
_DEFAULT_LOOKBACK_DAYS = 90
_DEFAULT_CLUSTER_THRESHOLD = 0.80
_PROGRESS_EVERY = 50  # emit a progress event every N drawers scanned

# Minimal English stopword list for term extraction
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "she",
        "so",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "that",
        "these",
        "those",
        "into",
        "also",
        "just",
        "now",
        "can",
        "all",
        "any",
        "via",
        "per",
        "than",
        "about",
        "each",
        "use",
        "used",
        "using",
        "set",
        "get",
        "add",
        "new",
        "one",
        "two",
        "after",
        "before",
        "during",
        "over",
        "under",
    }
)


# ── MCP helper ───────────────────────────────────────────────────────────────


def _mcp_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a MemPalace MCP tool via the CLI (module-level, patchable for tests)."""
    payload = json.dumps({"tool": tool_name, "arguments": args})
    try:
        result = subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


# ── Pure functions ────────────────────────────────────────────────────────────


def find_clusters(
    adjacency: dict[str, list[str]],
    min_size: int = 3,
) -> list[set[str]]:
    """BFS connected-component clustering over an adjacency list.

    Args:
        adjacency: Map from drawer_id → list of similar drawer_ids.
        min_size:  Minimum component size to include in results. Default 3.

    Returns:
        List of sets, each a connected component with ≥ min_size members.
        Ordering is deterministic (follows insertion order of adjacency keys).
    """
    visited: set[str] = set()
    clusters: list[set[str]] = []

    for start_node in adjacency:
        if start_node in visited:
            continue
        component: set[str] = set()
        queue: list[str] = [start_node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            queue.extend(n for n in adjacency.get(current, []) if n not in visited)
        if len(component) >= min_size:
            clusters.append(component)

    return clusters


def cluster_id(member_ids: set[str]) -> str:
    """Stable 12-char hex identifier for a cluster.

    Computed as SHA-256 of the sorted, pipe-joined member IDs.
    Same member set → same ID regardless of insertion order (idempotent).
    """
    key = "|".join(sorted(member_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def extract_common_terms(texts: list[str], top_n: int = 3) -> list[str]:
    """Extract the most frequent non-stopword terms from a list of texts.

    Tokenizes by splitting on non-alphanumeric characters, lowercases,
    filters stopwords and short tokens (< 3 chars), returns top_n.

    Args:
        texts:  List of text strings to analyze.
        top_n:  Maximum number of terms to return.

    Returns:
        List of up to top_n terms, ordered by frequency descending.
    """
    if not texts:
        return []
    counter: Counter[str] = Counter()
    for text in texts:
        tokens = re.split(r"[^a-z0-9]+", text.lower())
        for tok in tokens:
            if tok and len(tok) >= 3 and tok not in _STOPWORDS:
                counter[tok] += 1
    return [term for term, _ in counter.most_common(top_n)]


def classify_cluster(
    member_ids: set[str],
    categories: dict[str, str],
    texts: dict[str, str],
) -> tuple[str, str]:
    """Generate a human-readable label and dominant category for a cluster.

    Args:
        member_ids: Set of drawer IDs in the cluster.
        categories: Map from drawer_id → detected category (may be sparse).
        texts:      Map from drawer_id → text content (for term extraction).

    Returns:
        (label, dominant_category) where:
          label   — "{dominant_category} cluster: {terms} — {n} drawers"
          dominant_category — majority-vote category; "uncategorized" if absent.
        Tie-break on category vote: alphabetical order (stable).
    """
    # Dominant category: majority vote, tie-break alphabetical
    cat_votes: Counter[str] = Counter()
    for mid in member_ids:
        cat = categories.get(mid)
        if cat:
            cat_votes[cat] += 1

    if cat_votes:
        max_votes = max(cat_votes.values())
        # Collect all categories with max votes and pick alphabetically first
        tied = sorted(c for c, v in cat_votes.items() if v == max_votes)
        dominant = tied[0]
    else:
        dominant = "uncategorized"

    # Common terms from member texts
    member_texts = [texts.get(mid, "") for mid in member_ids]
    terms = extract_common_terms(member_texts, top_n=3)
    terms_str = ", ".join(terms) if terms else "mixed"

    label = f"{dominant} cluster: {terms_str} — {len(member_ids)} drawers"
    return label, dominant


# ── MCP orchestration ─────────────────────────────────────────────────────────


def _parse_drawer_ts(metadata: dict[str, Any]) -> datetime | None:
    """Best-effort extraction of a drawer's creation timestamp from metadata.

    Tries common field names in priority order: created_at, ts, added_at, date.
    Accepts ISO-8601 strings (with or without timezone) and numeric epoch seconds.
    Returns None if the field is absent or unparseable — callers should then
    include the drawer (best-effort: don't drop drawers we can't date).
    """
    for key in ("created_at", "ts", "added_at", "date"):
        raw = metadata.get(key)
        if raw is None:
            continue
        # Numeric epoch seconds (int or float)
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(raw), tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue
        # String: try ISO-8601 parse
        raw_str = str(raw).strip()
        if not raw_str:
            continue
        # datetime.fromisoformat handles YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, +TZ
        try:
            dt = datetime.fromisoformat(raw_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            pass
        # Epoch string like "1713369000"
        try:
            return datetime.fromtimestamp(float(raw_str), tz=UTC)
        except (ValueError, OSError, OverflowError):
            pass
    return None


def _get_drawers_in_scope(
    wing: str,
    room: str | None,
    max_drawers: int,
    lookback_days: int = 90,
) -> list[dict[str, Any]]:
    """Enumerate drawers in scope via taxonomy + search, applying the lookback filter.

    Best-effort lookback: drawers without a parseable timestamp are always included.
    Drawers with a timestamp older than ``lookback_days`` are excluded.

    Returns list of drawer dicts: {id, text, room, metadata}.
    """
    cutoff: datetime = datetime.now(UTC) - timedelta(days=lookback_days)
    drawers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Get taxonomy to discover rooms in scope
    taxonomy = _mcp_call("mempalace_get_taxonomy", {})
    wings_data = taxonomy.get("wings", [])

    # Find the scoped wing
    rooms_to_scan: list[str] = []
    for w in wings_data:
        if w.get("name") == wing:
            if room is not None:
                rooms_to_scan = [room]
            else:
                rooms_to_scan = [r["name"] for r in w.get("rooms", [])]
            break

    if not rooms_to_scan and room is not None:
        rooms_to_scan = [room]
    elif not rooms_to_scan:
        # Fallback: try a broad search without room scoping
        rooms_to_scan = ["__all__"]

    for r in rooms_to_scan:
        if len(drawers) >= max_drawers:
            break

        remaining = max_drawers - len(drawers)
        search_args: dict[str, Any] = {
            "query": r if r != "__all__" else wing,
            "wing": wing,
            "limit": min(50, remaining),
        }
        if r != "__all__":
            search_args["room"] = r

        # Try wildcard first, fall back to room-name query
        for query_val in ["*", r if r != "__all__" else wing]:
            search_args["query"] = query_val
            result = _mcp_call("mempalace_search", search_args)
            candidates = result.get("results", [])
            if candidates:
                break

        for c in candidates:
            did = c.get("id")
            if not did or did in seen_ids:
                continue
            if len(drawers) >= max_drawers:
                break

            # Best-effort lookback filter: drop drawers with a parseable
            # timestamp older than the cutoff. If timestamp is absent or
            # unparseable, include the drawer (never drop on ambiguity).
            meta = c.get("metadata", {})
            ts = _parse_drawer_ts(meta)
            if ts is not None and ts < cutoff:
                continue  # older than lookback window — skip

            seen_ids.add(did)
            drawers.append(
                {
                    "id": did,
                    "text": c.get("text", ""),
                    "room": c.get("room", r),
                    "metadata": meta,
                }
            )

    return drawers


def _build_adjacency(
    drawers: list[dict[str, Any]],
    cluster_threshold: float,
    emit_progress: Callable[[int, int], None] | None = None,
) -> dict[str, list[str]]:
    """Build adjacency list via mempalace_check_duplicate calls."""
    adjacency: dict[str, list[str]] = {d["id"]: [] for d in drawers}
    total = len(drawers)

    for i, drawer in enumerate(drawers):
        if i > 0 and i % _PROGRESS_EVERY == 0 and emit_progress:
            emit_progress(i, total)

        result = _mcp_call(
            "mempalace_check_duplicate",
            {
                "content": drawer["text"],
                "threshold": cluster_threshold,
            },
        )
        matches = result.get("matches", [])
        for m in matches:
            mid = m.get("id")
            if mid and mid != drawer["id"] and mid in adjacency:
                if mid not in adjacency[drawer["id"]]:
                    adjacency[drawer["id"]].append(mid)

    return adjacency


def _lookup_categories(
    drawer_ids: set[str],
) -> dict[str, str]:
    """Look up has_category KG facts for a set of drawer IDs."""
    categories: dict[str, str] = {}
    for did in drawer_ids:
        kg = _mcp_call("mempalace_kg_query", {"entity": f"drawer:{did}"})
        for fact in kg.get("facts", []):
            if fact.get("predicate") == "has_category" and fact.get("current", True):
                categories[did] = str(fact["object"])
                break
    return categories


def _lookup_importances(
    drawer_ids: set[str],
) -> dict[str, float]:
    """Return {drawer_id: importance} for drawers that already have has_importance KG facts."""
    importances: dict[str, float] = {}
    for did in drawer_ids:
        kg = _mcp_call("mempalace_kg_query", {"entity": f"drawer:{did}"})
        for fact in kg.get("facts", []):
            if fact.get("predicate") == "has_importance" and fact.get("current", True):
                try:
                    importances[did] = float(fact["object"])
                except (ValueError, TypeError):
                    pass
                break
    return importances


def execute_garden(
    wing: str | None,
    room: str | None,
    lookback_days: int,
    max_drawers: int,
    cluster_threshold: float,
    emit_fn: Callable[..., None] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Orchestrate a full garden run.

    Args:
        wing:              Wing to scope to. Required in practice; if None uses
                           best-effort from taxonomy.
        room:              Optional room within the wing.
        lookback_days:     Filter drawers by recency (metadata-based, best-effort).
        max_drawers:       Budget cap, already clamped to [1, 500] by caller.
        cluster_threshold: Cosine similarity threshold for clustering.
        emit_fn:           Callable matching emit_event signature (for progress).
        session_id:        Session ID for event emission.

    Returns:
        Result dict conforming to spec Section 6 output format.
    """
    effective_wing = wing or "unknown"
    kg_edges_created = 0

    # ── Step 1: Enumerate drawers ─────────────────────────────────────────
    drawers = _get_drawers_in_scope(effective_wing, room, max_drawers, lookback_days)
    n_drawers = len(drawers)

    if not drawers:
        return {
            "scope": {
                "wing": effective_wing,
                "room": room,
                "lookback_days": lookback_days,
            },
            "drawers_analyzed": 0,
            "clusters": [],
            "kg_edges_created": 0,
            "importance_backfilled": 0,
            "diary_entry": "skipped (no drawers found)",
        }

    # ── Step 2: Build adjacency via check_duplicate ───────────────────────
    def _emit_progress(scanned: int, total: int) -> None:
        if emit_fn:
            emit_fn(
                "tool-mempalace",
                "garden_progress",
                ok=True,
                data={"drawers_scanned": scanned, "drawers_total": total},
                session_id=session_id,
            )

    adjacency = _build_adjacency(drawers, cluster_threshold, _emit_progress)

    # ── Step 3: BFS clustering ────────────────────────────────────────────
    clusters = find_clusters(adjacency, min_size=3)

    # Collect all member IDs for KG lookups
    all_member_ids: set[str] = set()
    for c in clusters:
        all_member_ids.update(c)

    # ── Step 4: Classify clusters ─────────────────────────────────────────
    drawer_texts = {d["id"]: d["text"] for d in drawers}
    drawer_rooms = {d["id"]: d["room"] for d in drawers}

    categories = _lookup_categories(all_member_ids)

    cluster_results: list[dict[str, Any]] = []
    for cluster_members in clusters:
        cid = cluster_id(cluster_members)
        label, dominant_cat = classify_cluster(
            cluster_members, categories, drawer_texts
        )

        # Rooms spanned by this cluster
        rooms_in_cluster = sorted(
            {drawer_rooms.get(m, "") for m in cluster_members if drawer_rooms.get(m)}
        )

        cluster_results.append(
            {
                "id": f"cluster:{cid}",
                "label": label,
                "size": len(cluster_members),
                "dominant_category": dominant_cat,
                "rooms": rooms_in_cluster,
                "_members": cluster_members,  # internal, stripped before output
                "_cid": cid,
            }
        )

    # ── Steps 5 + 6: Emit KG edges ────────────────────────────────────────
    for cr in cluster_results:
        cid = cr["_cid"]
        members = cr["_members"]
        rooms_spanned = cr["rooms"]

        _mcp_call(
            "mempalace_kg_add",
            {
                "subject": f"cluster:{cid}",
                "predicate": "is_a",
                "object": "drawer_cluster",
            },
        )
        _mcp_call(
            "mempalace_kg_add",
            {
                "subject": f"cluster:{cid}",
                "predicate": "has_label",
                "object": cr["label"],
            },
        )
        _mcp_call(
            "mempalace_kg_add",
            {
                "subject": f"cluster:{cid}",
                "predicate": "has_size",
                "object": str(cr["size"]),
            },
        )
        kg_edges_created += 3

        for member_id in members:
            _mcp_call(
                "mempalace_kg_add",
                {
                    "subject": f"drawer:{member_id}",
                    "predicate": "part_of_cluster",
                    "object": f"cluster:{cid}",
                },
            )
            kg_edges_created += 1

        # Cross-room detection (only when room not scoped)
        if room is None and len(rooms_spanned) > 1:
            _mcp_call(
                "mempalace_kg_add",
                {
                    "subject": f"cluster:{cid}",
                    "predicate": "spans_rooms",
                    "object": ", ".join(rooms_spanned),
                },
            )
            kg_edges_created += 1

    # ── Step 8: Backfill importance ───────────────────────────────────────
    all_drawer_ids = {d["id"] for d in drawers}
    existing_importances = _lookup_importances(all_drawer_ids)
    missing_importance = all_drawer_ids - set(existing_importances)

    # Need categories for backfill — look up the ones we haven't fetched yet
    missing_cat_ids = missing_importance - set(categories)
    if missing_cat_ids:
        extra_cats = _lookup_categories(missing_cat_ids)
        categories.update(extra_cats)

    importance_backfilled = 0
    for did in missing_importance:
        cat = categories.get(did)
        score = compute_importance(cat, {})
        _mcp_call(
            "mempalace_kg_add",
            {
                "subject": f"drawer:{did}",
                "predicate": "has_importance",
                "object": str(score),
            },
        )
        kg_edges_created += 1
        importance_backfilled += 1

    # ── Step 7: Diary entry ───────────────────────────────────────────────
    largest_label = (
        max(cluster_results, key=lambda c: c["size"])["label"]
        if cluster_results
        else "none"
    )
    diary_text = (
        f"Garden run on {effective_wing} ({lookback_days}-day lookback): "
        f"analyzed {n_drawers} drawers, found {len(clusters)} clusters. "
        f"Largest: '{largest_label}'. "
        f"Created {kg_edges_created} KG edges. "
        f"{importance_backfilled} drawers had no importance tag (now tagged)."
    )
    _mcp_call(
        "mempalace_diary_write",
        {
            "agent_name": "curator",
            "entry": diary_text,
        },
    )

    # ── Strip internal fields from output ─────────────────────────────────
    output_clusters = [
        {k: v for k, v in cr.items() if not k.startswith("_")} for cr in cluster_results
    ]

    return {
        "scope": {"wing": effective_wing, "room": room, "lookback_days": lookback_days},
        "drawers_analyzed": n_drawers,
        "clusters": output_clusters,
        "kg_edges_created": kg_edges_created,
        "importance_backfilled": importance_backfilled,
        "diary_entry": "written",
    }
