"""
Memory Garden — on-demand deep analysis of memory store contents.

Spec: docs/spec-v1.2.0-gene-transfer.md Section 6.

Public API (used by MemoryTool.execute):
    execute_garden(wing, room, lookback_days, max_drawers,
                   cluster_threshold, emit_fn, session_id) -> dict

Pure-function helpers (importable for tests, no daemon calls):
    find_clusters(adjacency, min_size) -> list[set[str]]
    cluster_id(member_ids) -> str  (12-char hex)
    classify_cluster(member_ids, categories, texts) -> (label, dominant_category)
    extract_common_terms(texts, top_n) -> list[str]

Native cutover (B2, docs/plans/2026-07-07-native-cutover-design.md): drawer
enumeration, near-duplicate detection, KG edges, and diary writes all route
through ``MemoryClient`` via ``ensure_daemon()`` instead of the
vendor subprocess. Clustering math (the pure functions above) is
UNCHANGED.

Design notes:
- No taxonomy concept natively -- ``list_drawers`` enumerates a wing/room
  scope directly (no per-room MCP round trips needed).
- Near-duplicate detection is native ``search`` (hybrid cosine+lexical rank)
  scored against ``cluster_threshold``, replacing the old
  the legacy vendor's duplicate-check call.
- category/importance ride along on every ``list_drawers`` entry already --
  no separate per-drawer KG lookup round trip needed (the old
  ``_lookup_categories``/``_lookup_importances`` helpers are gone).
- Honest limitation: the native store does not track a drawer's filing
  timestamp, so ``lookback_days`` is currently a documented no-op (every
  drawer in scope is analyzed regardless of age) -- never silently
  fabricated. Revisit if/when the substrate grows a temporal-cell
  convention for filing time.
- Budget: max_drawers clamped to [1, 500]. Progress events every 50 drawers.
- Phase 3 rubric imported from .phase3 for importance backfill (Step 8).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from .client import MemoryClient, ensure_daemon
from .phase3 import compute_importance

# ── Constants ────────────────────────────────────────────────────────────

_MAX_DRAWERS_HARD_CAP = 500
_DEFAULT_MAX_DRAWERS = 200
_DEFAULT_LOOKBACK_DAYS = 90
_DEFAULT_CLUSTER_THRESHOLD = 0.80
_PROGRESS_EVERY = 50  # emit a progress event every N drawers scanned

#: How many candidates to request per per-drawer near-duplicate search.
_SEARCH_K = 20

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


# ── Pure functions ───────────────────────────────────────────────────────


def find_clusters(
    adjacency: dict[str, list[str]],
    min_size: int = 3,
) -> list[set[str]]:
    """BFS connected-component clustering over an adjacency list.

    Args:
        adjacency: Map from drawer_id -> list of similar drawer_ids.
        min_size:  Minimum component size to include in results. Default 3.

    Returns:
        List of sets, each a connected component with >= min_size members.
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
    Same member set -> same ID regardless of insertion order (idempotent).
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
        categories: Map from drawer_id -> detected category (may be sparse).
        texts:      Map from drawer_id -> text content (for term extraction).

    Returns:
        (label, dominant_category) where:
          label   -- "{dominant_category} cluster: {terms} -- {n} drawers"
          dominant_category -- majority-vote category; "uncategorized" if absent.
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

    label = f"{dominant} cluster: {terms_str} -- {len(member_ids)} drawers"
    return label, dominant


# ── Native drawer enumeration & near-duplicate detection ────────────────


def _get_drawers_in_scope(
    client: MemoryClient,
    wing: str,
    room: str | None,
    max_drawers: int,
) -> list[dict[str, Any]]:
    """Enumerate drawers in scope via the daemon's native ``list_drawers`` tool.

    Returns list of drawer dicts: ``{id, text, room, metadata}`` -- ``id``/
    ``text``/``room`` are lifted from ``list_drawers``' ``{ref, content,
    wing, room, category, importance}`` shape; category/importance ride
    along in ``metadata`` so downstream code needs no extra round trip.

    NOTE on lookback_days: intentionally NOT applied here -- the native
    store does not track a filing timestamp, so there is nothing honest to
    filter on (see module docstring). Every drawer ``list_drawers`` returns
    for the scope is considered in-scope.
    """
    raw = client.list_drawers(wing=wing, room=room, limit=max_drawers)
    drawers: list[dict[str, Any]] = []
    for d in raw:
        drawers.append(
            {
                "id": str(d.get("ref", "")),
                "text": d.get("content", "") or "",
                "room": d.get("room") or (room or "unknown"),
                "metadata": {
                    "category": d.get("category"),
                    "importance": d.get("importance"),
                },
            }
        )
    return drawers


def _build_adjacency(
    client: MemoryClient,
    drawers: list[dict[str, Any]],
    wing: str,
    room: str | None,
    cluster_threshold: float,
    emit_progress: Callable[[int, int], None] | None = None,
) -> dict[str, list[str]]:
    """Build a near-duplicate adjacency list via native hybrid search.

    Replaces the legacy vendor's duplicate-check call: for each drawer, search its own
    content within the same scope and treat any OTHER in-scope drawer
    scoring >= ``cluster_threshold`` as an edge. Best-effort -- a failed
    search for one drawer just leaves it with no edges (never aborts the
    whole garden run).
    """
    adjacency: dict[str, list[str]] = {d["id"]: [] for d in drawers}
    ids_in_scope = set(adjacency)
    total = len(drawers)

    for i, drawer in enumerate(drawers):
        if i > 0 and i % _PROGRESS_EVERY == 0 and emit_progress:
            emit_progress(i, total)

        try:
            out = client.search(drawer["text"][:1000], _SEARCH_K, wing=wing, room=room)
        except Exception:
            continue
        for hit in out.get("results", []) if isinstance(out, dict) else []:
            hid = str(hit.get("ref", ""))
            if not hid or hid == drawer["id"] or hid not in ids_in_scope:
                continue
            score = float(hit.get("score", 0.0))
            if score >= cluster_threshold and hid not in adjacency[drawer["id"]]:
                adjacency[drawer["id"]].append(hid)

    return adjacency


# ── Orchestration ────────────────────────────────────────────────────────


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
        wing:              Wing to scope to. Required in practice; if None
                           falls back to "unknown".
        room:              Optional room within the wing.
        lookback_days:     Carried through to the output ``scope`` dict for
                           API compatibility; currently a documented no-op
                           (see module docstring).
        max_drawers:       Budget cap, already clamped to [1, 500] by caller.
        cluster_threshold: Similarity threshold for clustering (native
                           ``search`` score, hybrid cosine+lexical).
        emit_fn:           Callable matching emit_event signature (for progress).
        session_id:        Session ID for event emission.

    Returns:
        Result dict conforming to spec Section 6 output format.

    Raises:
        RuntimeError: the memory daemon is unavailable (\u00a75.7 degradation
            contract -- the caller, ``MemoryTool.execute``'s "garden" branch,
            converts this into a loud ``ToolResult(success=False, ...)``).
    """
    client = ensure_daemon()
    if client is None:
        raise RuntimeError("memory daemon unavailable")

    effective_wing = wing or "unknown"
    kg_edges_created = 0

    # ── Step 1: Enumerate drawers ────────────────────────────────────
    drawers = _get_drawers_in_scope(client, effective_wing, room, max_drawers)
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

    # ── Step 2: Build adjacency via native search ────────────────────
    def _emit_progress(scanned: int, total: int) -> None:
        if emit_fn:
            emit_fn(
                "tool-memory",
                "garden_progress",
                ok=True,
                data={"drawers_scanned": scanned, "drawers_total": total},
                session_id=session_id,
            )

    adjacency = _build_adjacency(
        client, drawers, effective_wing, room, cluster_threshold, _emit_progress
    )

    # ── Step 3: BFS clustering ────────────────────────────────────────
    clusters = find_clusters(adjacency, min_size=3)

    # Collect all member IDs for category/label lookups
    all_member_ids: set[str] = set()
    for c in clusters:
        all_member_ids.update(c)

    # ── Step 4: Classify clusters ─────────────────────────────────────
    drawer_texts = {d["id"]: d["text"] for d in drawers}
    drawer_rooms = {d["id"]: d["room"] for d in drawers}
    categories = {
        d["id"]: d["metadata"]["category"]
        for d in drawers
        if d["metadata"].get("category")
    }

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

    # ── Steps 5 + 6: Emit KG edges ─────────────────────────────────────
    for cr in cluster_results:
        cid = cr["_cid"]
        members = cr["_members"]
        rooms_spanned = cr["rooms"]

        client.kg_add(f"cluster:{cid}", "is_a", "drawer_cluster")
        client.kg_add(f"cluster:{cid}", "has_label", cr["label"])
        client.kg_add(f"cluster:{cid}", "has_size", str(cr["size"]))
        kg_edges_created += 3

        for member_id in members:
            client.kg_add(f"drawer:{member_id}", "part_of_cluster", f"cluster:{cid}")
            kg_edges_created += 1

        # Cross-room detection (only when room not scoped)
        if room is None and len(rooms_spanned) > 1:
            client.kg_add(f"cluster:{cid}", "spans_rooms", ", ".join(rooms_spanned))
            kg_edges_created += 1

    # ── Step 8: Backfill importance directly on each drawer's own ref ──
    # (a plain has_importance fact on the drawer's real cell ref -- the SAME
    # convention NativeMemoryStore.file() itself uses -- not an
    # anchor-based KG entity, so this goes through the generic write_cell +
    # assert_fact primitives rather than kg_add.)
    all_drawer_ids = {d["id"] for d in drawers}
    existing_importance_ids = {
        d["id"] for d in drawers if d["metadata"].get("importance") is not None
    }
    missing_importance = all_drawer_ids - existing_importance_ids

    importance_backfilled = 0
    for did in missing_importance:
        cat = categories.get(did)
        score = compute_importance(cat, {})
        value_ref = client.write_cell(str(score).encode("utf-8"))
        client.assert_fact(did, "has_importance", value_ref)
        kg_edges_created += 1
        importance_backfilled += 1

    # ── Step 7: Diary entry ─────────────────────────────────────────────
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
    client.diary_write(agent_name="curator", entry=diary_text)

    # ── Strip internal fields from output ──────────────────────────────
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
