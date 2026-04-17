# Implementation Spec: v1.2.0 Gene Transfer

**Event Broadcast + Curator Compaction Heuristics**

Bundle: `amplifier-bundle-memory`
Base version: 1.1.0 → Target version: 1.2.0
Date: 2026-04-17

---

## Section 1: File Manifest

### New files

| File | Purpose |
|------|---------|
| `modules/tool-mempalace/amplifier_module_tool_mempalace/event_emitter.py` | Shared JSONL event emitter — all hooks import from here |

### Modified files

| File | What changes |
|------|-------------|
| `modules/tool-mempalace/amplifier_module_tool_mempalace/__init__.py` | Add `events` and `garden` operations to `PalaceTool`; add `events`/`garden` to `input_schema` enum; add event emission in `execute()` |
| `modules/tool-mempalace/pyproject.toml` | Bump version to 1.1.0; add `chromadb>=0.4.0` to dependencies (needed by `garden` clustering) |
| `modules/hooks-mempalace-capture/amplifier_module_hooks_mempalace_capture/__init__.py` | Import `emit_event` from emitter; add emission calls in `handle()` for `drawer_filed` and `capture_skipped` |
| `modules/hooks-mempalace-capture/pyproject.toml` | Bump version to 1.1.0; add `amplifier-module-tool-mempalace>=1.0.0` to dependencies |
| `modules/hooks-mempalace-briefing/amplifier_module_hooks_mempalace_briefing/__init__.py` | Import `emit_event`; add emission call for `briefing_assembled`; add importance re-ranking in `_build_briefing()`; fetch 8 results instead of 5 pre-rerank |
| `modules/hooks-mempalace-briefing/pyproject.toml` | Bump version to 1.1.0; add `amplifier-module-tool-mempalace>=1.0.0` to dependencies |
| `modules/hooks-mempalace-interject/amplifier_module_hooks_mempalace_interject/__init__.py` | Import `emit_event`; add emission calls for `memory_surfaced` and `interject_skipped` |
| `modules/hooks-mempalace-interject/pyproject.toml` | Bump version to 1.1.0; add `amplifier-module-tool-mempalace>=1.0.0` to dependencies |
| `modules/hooks-project-context/amplifier_module_hooks_project_context/__init__.py` | Import `emit_event`; add emission calls for `coordination_read`, `coordination_scaffolded`, `curator_delegated` |
| `modules/hooks-project-context/pyproject.toml` | Bump version to 1.1.0; add `amplifier-module-tool-mempalace>=1.0.0` to dependencies |
| `behaviors/mempalace.yaml` | Add config keys: `emit_events: true` on each hook; `briefing_importance_weight: 1.0` on briefing hook; `garden_max_drawers: 200` on tool-mempalace |
| `agents/curator.md` | Add Phase 3 instructions: duplicate linking, importance tagging, category tagging |
| `context/instructions.md` | Add sections on event observability and palace gardening |
| `skills/mempalace/SKILL.md` | Add `palace events` and `palace garden` quick-reference sections |
| `CHANGELOG.md` | Add v1.2.0 entry |

### Also modified (version bumps only)

| File | What changes |
|------|-------------|
| `bundle.md` | Bump `version: 1.1.0` → `1.2.0` in the bundle front-matter |
| `behaviors/mempalace.yaml` | Bump header `version: 1.2.0` → `1.3.0` (behavior version is independent of bundle version) |

### Files NOT changed

| File | Why |
|------|-----|
| `agents/archivist.md` | Read-path only; no changes needed |
| `context/project-context-guide.md` | No changes to coordination file protocol |

---

## Section 2: Event Schema (Canonical)

Every line in a session JSONL file is a single JSON object conforming to this schema. Schema version is pinned at `1` and must be incremented if breaking changes are made.

```json
{
  "v": 1,
  "ts": "2026-04-17T13:35:00.123456+00:00",
  "sid": "abc123def456",
  "hook": "mempalace-capture",
  "event": "drawer_filed",
  "ok": true,
  "preview": "Decided to use Clerk for auth because...",
  "data": {
    "wing": "wing_myapp",
    "room": "auth-migration-decision",
    "category": "decision",
    "content_bytes": 342,
    "dedupe_status": "unique"
  }
}
```

### Field definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `v` | `int` | yes | Schema version. Always `1` for this release. |
| `ts` | `string` | yes | ISO-8601 timestamp with timezone. Use `datetime.now(UTC).isoformat()`. |
| `sid` | `string` | yes | Session identifier. Extracted from `HookContext` or fallback (see Section 3). |
| `hook` | `string` | yes | Emitting hook name. One of: `mempalace-capture`, `mempalace-briefing`, `mempalace-interject`, `project-context`, `tool-mempalace`. |
| `event` | `string` | yes | Event type. Enumerated per hook below. |
| `ok` | `bool` | yes | `true` if the operation succeeded, `false` on error/skip. |
| `preview` | `string \| null` | yes | First 100 characters of the primary content involved. Truncated at grapheme boundary when possible, else byte boundary. `null` when no content applies (e.g., `briefing_assembled`). For binary-like content (non-UTF-8 decodable), set to `"[binary, {n} bytes]"`. |
| `data` | `object` | yes | Event-specific structured payload. Always an object, never null. |

### Event types per hook

**`mempalace-capture`**

| Event | ok | preview source | data fields |
|-------|-----|---------------|-------------|
| `drawer_filed` | `true` | drawer content | `wing: str`, `room: str`, `category: str\|null`, `content_bytes: int`, `source: str`, `dedupe_status: "unique"\|"near_duplicate"` |
| `capture_skipped` | `false` | tool output (if available) | `reason: str` (one of: `"too_short"`, `"too_long"`, `"skip_tool"`, `"category_filtered"`, `"mcp_error"`) |

**`mempalace-briefing`**

| Event | ok | preview source | data fields |
|-------|-----|---------------|-------------|
| `briefing_assembled` | `true` | `null` | `project: str`, `sections: list[str]`, `token_estimate: int`, `results_fetched: int`, `results_after_rerank: int`, `importance_weight: float` |
| `briefing_skipped` | `false` | `null` | `reason: str` (`"mempalace_unavailable"`, `"no_content"`) |

**`mempalace-interject`**

| Event | ok | preview source | data fields |
|-------|-----|---------------|-------------|
| `memory_surfaced` | `true` | injected memory text | `trigger: str` (`"prompt_submit"`, `"tool_pre"`, `"orchestrator_complete"`), `memory_ids: list[str]`, `top_score: float`, `judge_used: bool` |
| `interject_skipped` | `false` | `null` | `trigger: str`, `reason: str` (`"disabled"`, `"too_short"`, `"no_embedding"`, `"below_threshold"`, `"cooldown"`, `"guard_flag"`) |

**`project-context`**

| Event | ok | preview source | data fields |
|-------|-----|---------------|-------------|
| `coordination_read` | `true` | `null` | `files_read: list[str]`, `token_estimate: int` |
| `coordination_scaffolded` | `true` | `null` | `pc_dir: str`, `files_created: list[str]` |
| `curator_delegated` | `true` | `null` | `prompt_preview: str` |

**`tool-mempalace`**

| Event | ok | preview source | data fields |
|-------|-----|---------------|-------------|
| `garden_completed` | `true` | `null` | `scope_wing: str\|null`, `scope_room: str\|null`, `drawers_analyzed: int`, `clusters_found: int`, `kg_edges_created: int` |

### Preview truncation rules

1. If source text is ≤ 100 characters: use as-is.
2. If source text is > 100 characters: truncate to 97 characters + `"..."`.
3. If source text contains a newline before position 100: truncate at the first newline + `"..."`.
4. If source text is not valid UTF-8: `"[binary, {n} bytes]"`.
5. If no content applies to this event: `null`.

---

## Section 3: Emitter API

**File**: `modules/tool-mempalace/amplifier_module_tool_mempalace/event_emitter.py`

### Public API

```python
def emit_event(
    hook: str,
    event: str,
    *,
    ok: bool = True,
    preview: str | None = None,
    data: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Append a structured event to the session's JSONL event log.

    Thread-safe. Never raises — all errors are silently swallowed
    to avoid disrupting the hook that called us.
    """
```

```python
def read_events(
    session_id: str | None = None,
    *,
    hook_filter: str | None = None,
    event_filter: str | None = None,
    limit: int = 200,
    tail: bool = False,
) -> list[dict[str, Any]]:
    """Read events from a session's JSONL file.

    Returns a list of parsed event dicts. If tail=True, returns the
    last `limit` events. Otherwise returns the first `limit`.
    """
```

```python
def truncate_preview(text: str | None) -> str | None:
    """Apply the canonical preview truncation rules (Section 2)."""
```

### Session ID resolution

`emit_event` resolves session_id with this fallback chain:

1. Explicit `session_id` parameter (if provided).
2. `os.environ.get("AMPLIFIER_SESSION_ID")` — set by the Amplifier runtime.
3. `f"pid_{os.getpid()}_{date.today().isoformat()}"` — fallback for standalone testing.

The resolved session_id is cached at module level after first resolution (it doesn't change mid-session).

### File location

Events are written to: `~/.mempalace/events/{session_id}.jsonl`

- Directory `~/.mempalace/events/` is created on first write (`mkdir -p` equivalent).
- If `~/.mempalace/` does not exist (MemPalace not initialized), emit silently returns without writing.

### Write safety

- **Locking**: `threading.Lock` guards all writes. A single module-level lock instance ensures serialized access even if multiple hooks fire concurrently in different threads.
- **Append mode**: File opened with `mode="a"` (O_APPEND on POSIX) for every write, then closed. No long-lived file handle.
- **Flush**: `fh.write(line); fh.flush()` — enables `tail -f` consumers to see events immediately.
- **Compact JSON**: `json.dumps(record, separators=(",", ":"))` — one line per event, minimal size.
- **Error swallowing**: The entire `emit_event` body is wrapped in `try/except Exception: pass`. A failed event emission must never crash a hook.

### How hooks import

Each hook module adds to its `pyproject.toml`:

```toml
dependencies = [
    "amplifier-core>=0.1.0",
    "amplifier-module-tool-mempalace>=1.0.0",  # <-- new
    # ... existing deps
]
```

Import in each hook's `__init__.py`:

```python
from amplifier_module_tool_mempalace.event_emitter import emit_event, truncate_preview
```

### Session ID extraction per hook

Each hook extracts session_id from its context and passes it to `emit_event`:

```python
# Hook/HookContext pattern (capture, briefing, project-context):
sid = getattr(ctx, "session_id", None) or ctx.event.get("session_id")
emit_event("mempalace-capture", "drawer_filed", session_id=sid, ...)

# Coordinator pattern (interject):
sid = data.get("session_id")
emit_event("mempalace-interject", "memory_surfaced", session_id=sid, ...)
```

If `session_id` is not available from the context (older Amplifier versions), the fallback chain in the emitter handles it.

### File rotation / cleanup

Not in scope for v1.2.0. Session files are bounded by session duration (typically <1000 events, <500KB). Stale session files can be cleaned manually or by a future `palace(operation="garden", action="prune_events")`.

**Deferred**: `state.json` atomic snapshot file (from file-ipc-patterns skill). Not needed until a live UI consumer exists.

---

## Section 4: `palace events` Tool Operation

Added as a new operation in the existing `PalaceTool` class.

### Input schema addition

Add `"events"` to the `operation` enum in `PalaceTool.parameters`. New optional parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `session_id` | `string` | current session | Which session's events to read. |
| `hook_filter` | `string` | `null` | Filter to a specific hook (e.g., `"mempalace-capture"`). |
| `event_filter` | `string` | `null` | Filter to a specific event type (e.g., `"drawer_filed"`). |
| `tail` | `boolean` | `true` | If true, return the most recent events (last N). If false, return oldest first. |
| `limit` | `integer` | `50` | Max events to return. Capped at 200. |

### Output format

Returns a JSON object:

```json
{
  "session_id": "abc123",
  "event_count": 42,
  "returned": 42,
  "events": [ /* array of event objects per Section 2 schema */ ]
}
```

### Edge cases

| Case | Behavior |
|------|----------|
| No events file for session | Return `{"session_id": "...", "event_count": 0, "returned": 0, "events": []}` |
| `~/.mempalace/events/` doesn't exist | Same as above |
| Corrupt line in JSONL | Skip the line, continue reading. Log count of skipped lines in a `"skipped_lines"` field. |
| Session ID not provided and not resolvable | Use the fallback session ID from the emitter module |

### Implementation location

New `elif operation == "events":` branch in `PalaceTool.execute()`, calling `read_events()` from the emitter module.

---

## Section 5: Curator Phase 3 (Lightweight, session:end)

### Where it lives

Updated instructions in `agents/curator.md`. Phase 3 runs after Phase 2 (coordination file updates) at every session:end invocation.

### Algorithm

**Input**: The set of drawers filed during Phase 1 of the current session run.

**Step 1 — Duplicate linking** (per drawer filed in Phase 1):

The curator already calls `mempalace_check_duplicate` in Phase 1 before filing. Phase 3 changes the response to duplicate detection:

| Duplicate score | Phase 1 action (CHANGED) | Phase 3 KG action |
|----------------|--------------------------|-------------------|
| ≥ 0.95 (near-identical) | **File both** (was: skip). Mark new drawer importance as `low`. | `mempalace_kg_add(subject="drawer:<new_id>", predicate="duplicates", object="drawer:<existing_id>")` |
| 0.85 – 0.94 (related) | File both (unchanged). | `mempalace_kg_add(subject="drawer:<new_id>", predicate="related_to", object="drawer:<match_id>")` |
| < 0.85 (distinct) | File normally (unchanged). | No edge. |

The duplicate check uses the existing `mempalace_check_duplicate` MCP tool which performs cosine similarity against the palace's ChromaDB embeddings internally. No additional embedding computation needed.

**Step 2 — Importance tagging** (per drawer filed):

Score importance 0.0–1.0 based on category and signals, then record as a KG fact:

| Category | Base importance | Boost conditions | Cap |
|----------|----------------|-----------------|-----|
| `decision` | 0.75 | +0.10 if architecture-level, +0.15 if user-explicit | 1.0 |
| `architecture` | 0.70 | +0.10 if cross-wing | 1.0 |
| `blocker` | 0.65 | +0.10 if unresolved at session end | 0.9 |
| `resolved_blocker` | 0.55 | +0.10 if root cause documented | 0.8 |
| `dependency` | 0.50 | +0.10 if external/breaking | 0.8 |
| `pattern` | 0.50 | +0.10 if cross-project | 0.8 |
| `lesson_learned` | 0.45 | +0.10 if gotcha-adjacent | 0.7 |
| uncategorized | 0.40 | none | 0.5 |

For drawers flagged as near-identical duplicates (score ≥ 0.95): override importance to 0.15 (`low`).

KG fact:
```
mempalace_kg_add(subject="drawer:<id>", predicate="has_importance", object="<score>")
```

**Step 3 — Category tagging** (per drawer filed):

```
mempalace_kg_add(subject="drawer:<id>", predicate="has_category", object="<category>")
```

Only if a category was detected. Uncategorized drawers get no category KG fact.

### Idempotency

If Phase 3 runs twice on the same session (e.g., manual re-invocation):

- `mempalace_kg_add` with identical subject/predicate/object is a no-op in MemPalace (upsert semantics for KG facts with the same triple).
- The curator instructions explicitly state: "Before adding a KG edge, check if the same triple already exists via `mempalace_kg_query`. Skip if present."

### Cost

Per drawer filed: 1 `mempalace_check_duplicate` (already in Phase 1) + 1–3 `mempalace_kg_add` calls (Phase 3). Total additional MCP calls per session: ~3× number of drawers filed. At typical session volumes (5–20 drawers), this adds 15–60 subprocess calls at ~100ms each = 1.5–6 seconds. Acceptable for session:end.

### Curator agent instruction additions

Add after the existing Phase 2 section:

```markdown
**Phase 3 — Palace intelligence (KG enrichment):**

10. For each drawer filed in Phase 1, record its importance and category in the knowledge graph:
    - Call `mempalace_kg_add(subject="drawer:<id>", predicate="has_importance", object="<score>")` using the importance rubric above.
    - Call `mempalace_kg_add(subject="drawer:<id>", predicate="has_category", object="<category>")` if a category was detected.
11. For any drawer where `mempalace_check_duplicate` returned a match:
    - If match score ≥ 0.95: file both drawers, set the newer drawer's importance to 0.15, and add `mempalace_kg_add(subject="drawer:<new_id>", predicate="duplicates", object="drawer:<match_id>")`.
    - If match score 0.85–0.94: add `mempalace_kg_add(subject="drawer:<new_id>", predicate="related_to", object="drawer:<match_id>")`.
12. Idempotency: before adding any KG edge, check via `mempalace_kg_query` that the triple does not already exist. Skip if present.
```

---

## Section 6: `palace garden` Operation (On-Demand, Deep)

### Input schema addition

Add `"garden"` to the `operation` enum. New parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `wing` | `string` | auto-detected | Scope to a specific wing. Strongly recommended. |
| `room` | `string` | `null` | Further scope to a specific room. |
| `lookback_days` | `integer` | `90` | Only analyze drawers added in the last N days. |
| `max_drawers` | `integer` | `200` | Budget cap. Max drawers to analyze per run. |
| `cluster_threshold` | `float` | `0.80` | Cosine similarity threshold for clustering. |

### Algorithm

**Step 1 — Enumerate drawers in scope**

```
taxonomy = mempalace_get_taxonomy()  # get wing/room structure with counts
```

For each room in the scoped wing (or all rooms if no room filter):
- Call `mempalace_search(query="*", wing=<wing>, room=<room>, limit=50)` to retrieve drawers.
- Collect drawer IDs, texts, and metadata.
- Stop collecting when `max_drawers` is reached.

If `mempalace_search` with `"*"` doesn't return all drawers (semantic search may not match a wildcard), use `mempalace_search` with the room name as the query as a broad-match fallback.

**Step 2 — Pairwise similarity via check_duplicate**

For each collected drawer (up to `max_drawers`):

```
matches = mempalace_check_duplicate(content=<drawer_text>, threshold=<cluster_threshold>)
```

Build an adjacency list: `adjacency[drawer_id] = [match_id for match in matches.matches]`.

**Budget guardrail**: This step makes at most `max_drawers` MCP subprocess calls. At 200 drawers × ~150ms/call = ~30 seconds. The tool emits a progress event every 50 drawers.

**Step 3 — Connected component clustering**

Simple BFS over the adjacency list to find connected components:

```python
def find_clusters(adjacency: dict[str, list[str]]) -> list[set[str]]:
    visited = set()
    clusters = []
    for node in adjacency:
        if node in visited:
            continue
        component = set()
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            queue.extend(n for n in adjacency.get(current, []) if n not in visited)
        if len(component) >= 3:  # only clusters with 3+ members
            clusters.append(component)
    return clusters
```

No external dependency. Pure Python BFS. O(V + E) time.

**Step 4 — Classify clusters**

For each cluster:
1. Determine dominant category by majority vote of members' `has_category` KG facts.
2. Generate a human-readable cluster label: `"{category} cluster: {common_terms} — {n} drawers"`.
   - `common_terms`: extract 2–3 most frequent non-stopword terms from member texts (simple term-frequency, no NLP library needed).

**Step 5 — Emit KG edges**

For each cluster with label `cluster_label`:

```
# Create the cluster entity
mempalace_kg_add(subject="cluster:<hash>", predicate="is_a", object="drawer_cluster")
mempalace_kg_add(subject="cluster:<hash>", predicate="has_label", object="<cluster_label>")
mempalace_kg_add(subject="cluster:<hash>", predicate="has_size", object="<n>")

# Link members
for drawer_id in cluster:
    mempalace_kg_add(subject="drawer:<drawer_id>", predicate="part_of_cluster", object="cluster:<hash>")
```

The `<hash>` is a stable identifier: `hashlib.sha256(sorted_member_ids_joined).hexdigest()[:12]`. This ensures re-running garden on the same data produces the same cluster ID (idempotent).

**Step 6 — Cross-wing detection** (only if `room` is not scoped)

For clusters whose members span multiple rooms:

```
mempalace_kg_add(subject="cluster:<hash>", predicate="spans_rooms", object="<room1>, <room2>")
```

**Step 7 — Write curator diary entry**

```
mempalace_diary_write(
    agent_name="curator",
    entry="Garden run on wing_myapp (90-day lookback): analyzed 187 drawers, "
          "found 4 clusters. Largest: 'decision cluster: auth migration — 8 drawers'. "
          "Created 23 KG edges. 12 drawers had no importance tag (now tagged)."
)
```

**Step 8 — Backfill importance tags**

For any drawer in scope that lacks a `has_importance` KG fact:
- Apply the importance rubric from Section 5.
- Add the KG fact.

This ensures garden gradually enriches the entire palace, not just drawers filed after v1.2.0.

### Output format

```json
{
  "scope": {"wing": "wing_myapp", "room": null, "lookback_days": 90},
  "drawers_analyzed": 187,
  "clusters": [
    {
      "id": "cluster:a3f2b1c9d0e4",
      "label": "decision cluster: auth migration — 8 drawers",
      "size": 8,
      "dominant_category": "decision",
      "rooms": ["auth-migration-decision", "decisions"]
    }
  ],
  "kg_edges_created": 23,
  "importance_backfilled": 12,
  "diary_entry": "written"
}
```

### Budget and safety

| Guard | Value | Rationale |
|-------|-------|-----------|
| `max_drawers` | 200 (config, hard cap 500) | Bounds MCP subprocess calls to ≤500 |
| Timeout per MCP call | 15s | Prevents hanging on unresponsive palace |
| Total operation timeout | 120s | PalaceTool already uses timeout=120 for mine |
| Progress events | Every 50 drawers | Enables UI feedback during long runs |

---

## Section 7: Briefing Importance Integration

### Current flow (unchanged parts)

1. `mempalace_search(query, wing, limit=5)` → results sorted by semantic similarity.
2. Format results into briefing sections.
3. Apply token budget.

### New flow

1. `mempalace_search(query, wing, limit=8)` → fetch extra candidates for re-ranking headroom.
2. For each result, look up importance from KG:
   ```
   kg_result = mempalace_kg_query(entity="drawer:<result_id>")
   importance = extract has_importance fact, default 0.5 if absent
   ```
3. Compute final score per result:
   ```
   final = semantic_score + briefing_importance_weight * (importance - 0.5) * 0.08
   ```
4. Sort by `final` descending.
5. Take top 5.
6. Format and apply token budget (unchanged).

### Formula properties

| Parameter | Value | Effect |
|-----------|-------|--------|
| `briefing_importance_weight` | config float, default `1.0` | Multiplier on importance signal. `0.0` = disabled (pure semantic). |
| Scaling constant | `0.08` | Hardcoded. Bounds max boost to ±0.04 at weight=1.0. |
| Neutral importance | `0.5` | Drawers without KG importance facts default here → zero boost. |

**Worked examples at weight=1.0:**

| Drawer | Semantic | Importance | Boost | Final | Note |
|--------|----------|-----------|-------|-------|------|
| A | 0.92 | 0.50 (default) | 0.000 | 0.920 | Untagged: no change |
| B | 0.90 | 0.80 | +0.024 | 0.924 | High importance boosts B above A |
| C | 0.88 | 1.00 (critical) | +0.040 | 0.920 | Ties with A; does not jump above B |
| D | 0.85 | 0.15 (low/dup) | -0.028 | 0.822 | Low importance sinks duplicate |
| E | 0.80 | 1.00 (critical) | +0.040 | 0.840 | Max boost. Still below A, B, C. |

**Key property**: A result's importance can never overcome a semantic gap > 0.04 (at weight=1.0). Semantic similarity dominates. Importance only reorders within a tight band.

**Zero-regression guarantee for untagged palaces**: When no drawers have `has_importance` KG facts, all default to 0.5, all boosts are 0.0, and ranking is identical to current v1.1.0 behavior.

### KG lookup optimization

Potential concern: 8 additional `mempalace_kg_query` subprocess calls at session:start.

Mitigation: batch the KG lookups. Instead of 8 individual calls, use a single `mempalace_kg_query` with entity prefix `"drawer:"` if the MCP tool supports wildcard/prefix queries. If not, the 8 calls at ~100ms each add ~800ms to briefing assembly — acceptable given briefing already makes 3–4 MCP calls.

If the latency is unacceptable in practice, fall back to: skip KG lookups entirely if `briefing_importance_weight == 0.0` (the config check is free).

### Implementation location

Modify `_build_briefing()` in `hooks-mempalace-briefing/__init__.py`:

1. Change `limit` from 5 to 8 in the `mempalace_search` call.
2. After getting results, add the re-ranking block.
3. Truncate to top 5 after re-ranking.
4. Read `briefing_importance_weight` from `self.config`.

---

## Section 8: Test Strategy

All tests live in `tests/` directories within each modified module. Test runner: `pytest` with `asyncio_mode = "auto"`.

### 8.1 Event Emitter Unit Tests

**File**: `modules/tool-mempalace/tests/test_event_emitter.py`

| Test | What it verifies |
|------|-----------------|
| `test_emit_creates_directory_and_file` | First emit creates `~/.mempalace/events/` and the session JSONL file. |
| `test_emit_appends_valid_jsonl` | Each emit produces exactly one JSON-parseable line with all required fields. |
| `test_emit_schema_version` | Every emitted line has `"v": 1`. |
| `test_emit_concurrent_safety` | 100 emits from 10 threads → all 100 lines present, no corruption. Uses `ThreadPoolExecutor`. |
| `test_emit_never_raises` | Emit with read-only directory → no exception, returns silently. |
| `test_emit_no_mempalace_dir` | If `~/.mempalace/` doesn't exist → silent no-op. |
| `test_preview_truncation_short` | Text ≤ 100 chars → preserved exactly. |
| `test_preview_truncation_long` | Text > 100 chars → 97 chars + `"..."`. |
| `test_preview_truncation_newline` | Text with newline at pos 50 → truncated at newline + `"..."`. |
| `test_preview_null` | `None` input → `None` output. |
| `test_read_events_basic` | Write 5 events, read back → 5 events with correct structure. |
| `test_read_events_tail` | Write 10 events, read with `tail=True, limit=3` → last 3 events. |
| `test_read_events_filter` | Write mixed events, filter by hook → only matching events returned. |
| `test_read_events_missing_file` | Read from nonexistent session → empty list. |
| `test_session_id_fallback` | No env var, no explicit ID → falls back to `pid_*` format. |

### 8.2 Palace Events Tool Operation Tests

**File**: `modules/tool-mempalace/tests/test_palace_events.py`

| Test | What it verifies |
|------|-----------------|
| `test_events_operation_registered` | `"events"` in operation enum, PalaceTool accepts it. |
| `test_events_returns_json` | Calling `execute(operation="events")` returns valid JSON with `session_id`, `event_count`, `events`. |
| `test_events_with_filters` | `hook_filter` and `event_filter` correctly narrow results. |
| `test_events_empty_session` | Unknown session_id → `event_count: 0`, no error. |

### 8.3 Palace Garden Tool Operation Tests

**File**: `modules/tool-mempalace/tests/test_palace_garden.py`

| Test | What it verifies |
|------|-----------------|
| `test_garden_operation_registered` | `"garden"` in operation enum. |
| `test_find_clusters_basic` | `find_clusters()` with known adjacency → correct connected components. Pure unit test, no MCP. |
| `test_find_clusters_minimum_size` | Components with < 3 members → excluded from results. |
| `test_find_clusters_empty` | Empty adjacency → empty result. |
| `test_cluster_id_stable` | Same member set → same cluster hash. Idempotency. |
| `test_garden_budget_cap` | `max_drawers=10` → at most 10 MCP calls (mock subprocess). |

### 8.4 Curator Phase 3 Integration Test

**File**: `modules/tool-mempalace/tests/test_curator_phase3.py`

This tests the KG enrichment logic that the curator agent executes. Since Phase 3 is agent instructions (not Python code), the integration test validates the MCP tool calls that Phase 3 produces.

| Test | What it verifies |
|------|-----------------|
| `test_duplicate_linking_high` | Seed palace with 2 drawers at 0.96 similarity. Run Phase 3 logic. Verify `duplicates` KG edge and `has_importance: 0.15` on newer. |
| `test_duplicate_linking_medium` | 2 drawers at 0.88 similarity → `related_to` edge, no importance override. |
| `test_duplicate_linking_distinct` | 2 drawers at 0.70 similarity → no KG edge. |
| `test_importance_scoring` | File drawers of each category. Verify importance scores match the rubric. |
| `test_idempotency` | Run Phase 3 twice. Verify no duplicate KG facts. |

**Fixture**: A seeded MemPalace instance at a temp directory (`tmp_path / ".mempalace"`) with pre-loaded drawers. Uses `mempalace init` + `mempalace mcp --call` to seed.

### 8.5 Briefing Re-Ranking Unit Tests

**File**: `modules/hooks-mempalace-briefing/tests/test_importance_rerank.py`

| Test | What it verifies |
|------|-----------------|
| `test_rerank_no_importance_facts` | All drawers at default 0.5 → order unchanged from semantic. |
| `test_rerank_high_importance_boost` | Drawer with importance=0.9 at semantic=0.85 → jumps above drawer with default importance at semantic=0.86. |
| `test_rerank_low_importance_sink` | Drawer with importance=0.15 at semantic=0.90 → sinks below drawer with default at semantic=0.89. |
| `test_rerank_weight_zero_disabled` | `briefing_importance_weight=0.0` → order identical to semantic. |
| `test_rerank_max_boost_bounded` | importance=1.0, weight=1.0 → boost exactly 0.04. |
| `test_rerank_preserves_top_result` | The top semantic result (score 0.95) is never displaced by an importance boost alone. |

All re-ranking tests use fixed inputs (no MCP calls) — they test the formula, not the KG lookup.

### 8.6 Hook Emission Integration Tests

**File**: `tests/test_hook_emissions.py` (bundle-level)

| Test | What it verifies |
|------|-----------------|
| `test_capture_emits_on_file` | Mock a tool:post event → capture hook fires → JSONL file contains `drawer_filed` event. |
| `test_capture_emits_on_skip` | Mock a too-short tool output → JSONL file contains `capture_skipped` event. |
| `test_briefing_emits_on_assemble` | Mock session:start → briefing hook fires → `briefing_assembled` event. |
| `test_interject_emits_on_surface` | Mock prompt:submit with matching memory → `memory_surfaced` event. |
| `test_project_context_emits_on_read` | Mock session:start with project-context/ → `coordination_read` event. |

---

## Section 9: Benchmark Validation Plan

### 9.1 What we're protecting

The bundle claims 96.6% R@5 on LongMemEval. This benchmark is a property of MemPalace's underlying retrieval engine, not the briefing hook's ranking. However, the briefing hook mediates what the agent sees at session:start. If importance re-ranking degrades the quality of that briefing, it's a functional regression even if MemPalace's raw R@5 is unchanged.

### 9.2 Benchmark approach: Synthetic Recall Test

LongMemEval requires a specific evaluation harness and dataset. We define a local proxy benchmark that tests the same property (correct memories in top-K) on our own data.

**Fixture: The Recall Test Palace**

Seeded with 200 drawers across 4 wings and 20 rooms. Content is synthetic but realistic (decision records, code snippets, error logs, architecture notes). Each drawer has a deterministic ID.

**Query set**: 30 queries with known ground-truth drawer IDs (manually curated). Example:

```yaml
- query: "Why did we switch from REST to GraphQL?"
  expected_ids: ["d_042", "d_043", "d_099"]
  wing: "wing_myapp"
```

**Metric**: R@5 — the fraction of expected drawers that appear in the top-5 results. Averaged across all 30 queries.

### 9.3 Before/after protocol

1. **Seed the test palace** (once, deterministic, committed as a test fixture).
2. **Run baseline** (importance_weight=0.0 → pure semantic ranking):
   - For each query, call `_build_briefing()` and capture the top-5 result IDs.
   - Compute R@5_baseline.
3. **Run with importance re-ranking** (importance_weight=1.0):
   - First, backfill importance KG facts for all 200 drawers using the Phase 3 rubric.
   - For each query, call `_build_briefing()` and capture the top-5 result IDs.
   - Compute R@5_reranked.
4. **Assert**: `R@5_reranked >= R@5_baseline`.
5. **Record both scores** in `docs/eval/briefing-rerank-benchmark.md`.

### 9.4 Acceptance criteria

| Criterion | Threshold | Action if failed |
|-----------|-----------|-----------------|
| R@5 no-regression | `R@5_reranked >= R@5_baseline` | Reduce `importance_weight` default or tighten scaling constant. |
| R@5 improvement | `R@5_reranked > R@5_baseline` | Note improvement magnitude in CHANGELOG. |
| Briefing latency | `< 3.0s` for 1500-token briefing | Optimize KG lookups (batch or cache). |

### 9.5 Zero-change baseline verification

Before testing importance re-ranking, verify that the code changes alone (without importance KG facts) produce identical results to v1.1.0:

- Run the query set with importance_weight=1.0 but no `has_importance` KG facts in the palace.
- All drawers default to importance=0.5, boost=0.0.
- Assert: top-5 results are **identical** to v1.1.0 baseline. Byte-for-byte ID match.

This proves the change is a strict no-op on untagged palaces.

### 9.6 Ongoing regression test

Add the benchmark as a `pytest` test (`tests/test_benchmark_recall.py`) that runs with `pytest -m benchmark` (marked slow, excluded from default test runs). CI can run it on release branches.

---

## Section 10: Rollout Order

Six checkpoints, each independently shippable.

### Checkpoint 1: Event Emitter + Emission Points

**Ship**: `event_emitter.py` + emission calls in all 5 hooks.
**Validates**: `test_event_emitter.py` + `test_hook_emissions.py` all pass.
**Value**: Full activity log. `tail -f ~/.mempalace/events/*.jsonl` works immediately.
**Risk**: None. Emit-only, no behavioral changes. Silent on error.

### Checkpoint 2: `palace events` Tool Operation

**Ship**: New `events` operation in PalaceTool.
**Validates**: `test_palace_events.py` passes.
**Value**: Agents and users can query the event log via the tool interface.
**Risk**: None. Read-only operation on a file that already exists from Checkpoint 1.
**Depends on**: Checkpoint 1.

### Checkpoint 3: Curator Phase 3

**Ship**: Updated `agents/curator.md` with Phase 3 instructions.
**Validates**: `test_curator_phase3.py` integration tests pass.
**Value**: Every session:end now enriches the KG with importance/category/duplicate edges.
**Risk**: Low. Additive KG facts only. No existing behavior changes. If Phase 3 fails, Phase 1 and 2 are unaffected (agent instructions are sequential, not conditional).
**Depends on**: Nothing (but pairs well with Checkpoint 4).

### Checkpoint 4: Briefing Importance Integration

**Ship**: Re-ranking logic in `hooks-mempalace-briefing`.
**Validates**: `test_importance_rerank.py` unit tests pass. Benchmark (Section 9) passes.
**Value**: Briefings surface high-importance memories preferentially.
**Risk**: **Highest risk checkpoint.** This is the one that could regress retrieval quality. Ship with `briefing_importance_weight: 1.0` in config but document the `0.0` escape hatch.
**Depends on**: Checkpoint 3 (needs importance KG facts to be useful).

### Checkpoint 5: `palace garden` Tool Operation

**Ship**: New `garden` operation in PalaceTool + clustering logic.
**Validates**: `test_palace_garden.py` passes.
**Value**: On-demand deep analysis of palace contents. Pattern detection. Backfill for pre-v1.2.0 drawers.
**Risk**: Low. On-demand only. Bounded by `max_drawers`. Additive KG edges.
**Depends on**: Nothing (but results are more useful after Checkpoint 3 populates importance facts).

### Checkpoint 6: Behavior YAML + Docs + CHANGELOG

**Ship**: Updated `behaviors/mempalace.yaml`, `context/instructions.md`, `skills/mempalace/SKILL.md`, `CHANGELOG.md`.
**Validates**: Bundle loads without errors. All config keys parse correctly.
**Value**: Documentation and discoverability.
**Depends on**: All previous checkpoints.

---

## Section 11: Feature Flags / Gradual Enablement

All flags are config keys in `behaviors/mempalace.yaml`, settable per-hook.

### Event emission

```yaml
- module: hooks-mempalace-capture
  source: mempalace:modules/hooks-mempalace-capture
  config:
    emit_events: true          # NEW: false to disable event emission
    # ... existing config
```

Applied to all 4 hook modules and tool-mempalace. Default: `true`.

**Implementation**: Each hook checks `self.config.get("emit_events", True)` before calling `emit_event()`. If `false`, skip the call entirely. Zero cost when disabled.

### Briefing importance re-ranking

```yaml
- module: hooks-mempalace-briefing
  source: mempalace:modules/hooks-mempalace-briefing
  config:
    briefing_importance_weight: 1.0  # NEW: 0.0 disables importance signal
    # ... existing config
```

Default: `1.0`. Set to `0.0` to get pure semantic ranking (identical to v1.1.0 behavior).

**Implementation**: `_build_briefing()` reads `self.briefing_importance_weight` and skips the KG lookup + re-ranking block entirely when weight is 0.0. No additional MCP calls, no latency added.

### Garden budget

```yaml
- module: tool-mempalace
  source: mempalace:modules/tool-mempalace
  config:
    garden_max_drawers: 200     # NEW: budget cap for garden analysis
    # ... existing config
```

Default: `200`. Hard cap: `500` (enforced in code regardless of config).

### Curator Phase 3

Phase 3 is agent instructions, not code. To disable: the user edits `agents/curator.md` and removes or comments out the Phase 3 section. There is no config flag.

**Justification**: Phase 3 is 3 paragraphs of markdown instructions. A config flag would require the agent to check a runtime config value, which adds complexity to the agent prompt for minimal benefit. If Phase 3 causes issues, removing the instructions is immediate and obvious.

### Summary of knobs

| Knob | Location | Default | Disables |
|------|----------|---------|----------|
| `emit_events` | each hook config | `true` | Event emission for that hook |
| `briefing_importance_weight` | briefing hook config | `1.0` | Importance re-ranking (`0.0` = off) |
| `garden_max_drawers` | tool-mempalace config | `200` | N/A (budget, not on/off) |
| Phase 3 instructions | `agents/curator.md` | present | Remove text to disable |

---

## Section 12: CHANGELOG Entry

```markdown
## [1.2.0] — 2026-04-XX

### Added
- **Event observability**: All memory hooks now emit structured events to a per-session
  JSONL log at `~/.mempalace/events/{session_id}.jsonl`. Events include a ~100-char
  content preview and structured metadata. New `palace(operation="events")` tool reads
  the log with optional hook/event filtering and tail mode.
- **Curator Phase 3 — palace intelligence**: The Curator now enriches the knowledge graph
  at session:end with importance scores, category tags, and duplicate/related-to edges
  for every drawer filed during the session. Near-identical duplicates (cosine ≥ 0.95)
  are filed verbatim (preserving the never-delete guarantee) and linked via KG.
- **`palace garden` operation**: On-demand deep analysis of palace contents. Detects
  content clusters via BFS over pairwise cosine similarity, records them as KG entities,
  backfills importance tags on pre-v1.2.0 drawers, and writes a curator diary entry
  summarizing findings. Budget-capped at 200 drawers per run (configurable).
- **Importance-ranked briefings**: The session:start briefing now applies a soft importance
  boost to re-rank search results. Semantic similarity remains the primary signal;
  importance acts as a tiebreak within a ±0.04 score band. Drawers without importance
  tags are unaffected (zero-boost). Configurable via `briefing_importance_weight`
  (default 1.0, set to 0.0 to disable).
- **Shared event emitter module**: `event_emitter.py` in `tool-mempalace` provides
  thread-safe, never-failing JSONL append with canonical schema (v1).

### Changed
- **Curator agent**: Phase 1 now files both drawers when a near-duplicate is detected
  (was: skip the new drawer). The new drawer gets importance `0.15` and a `duplicates`
  KG edge. This aligns with the verbatim-never-delete philosophy.
- **Briefing hook**: Fetches 8 search results (was 5) to allow re-ranking headroom,
  then trims to top 5 after importance boost.
- **All hook modules** now depend on `amplifier-module-tool-mempalace>=1.0.0` for the
  shared event emitter.

### Configuration
- `emit_events: true|false` — per-hook flag to disable event emission (default: true).
- `briefing_importance_weight: float` — importance signal strength (default: 1.0, 0.0 = off).
- `garden_max_drawers: int` — budget cap for garden analysis (default: 200, hard cap: 500).
```

---

## Appendix A: Deferred Features

| Feature | Why deferred |
|---------|-------------|
| SSE/WebSocket event transport | No consumer exists. JSONL file + `tail -f` is sufficient. Build the transport when a live UI materializes. |
| `state.json` atomic snapshot | Useful for UI polling but no UI exists. The JSONL log is the complete record. |
| Scheduled/cron gardening | On-demand is enough. Automated runs risk churning on small palaces. |
| Cold-wing archival | Importance-ranked briefing achieves 80% of the noise reduction. Moving drawers to a cold ChromaDB collection is mechanically complex for marginal gain. |
| Event-driven UI dashboard | The JSONL log is the data contract. A dashboard is a separate project. |
| Merge/summarize for duplicates | Violates verbatim-never-summarize. Linking via KG is the MemPalace-native alternative. |
| Auto-deletion of any kind | Not in scope now, not in scope ever. Design is strictly additive. |
| Cross-wing garden analysis | Comparing drawers across wings adds O(wings²) cost. Scope to single wing for v1.2.0. |
| Never-delete protection rules | The -alt curator had "never delete decisions/gotchas." In MemPalace, nothing is deleted, so the rules are moot. If archival is added later, these rules become relevant. |

## Appendix B: Dependency Graph After v1.2.0

```
hooks-mempalace-capture ──→ tool-mempalace (event_emitter.py)
hooks-mempalace-briefing ─→ tool-mempalace (event_emitter.py)
hooks-mempalace-interject → tool-mempalace (event_emitter.py)
hooks-project-context ────→ tool-mempalace (event_emitter.py)

tool-mempalace ───→ mempalace (CLI), chromadb (garden only)
```

All 4 hook modules gain a new dependency on `tool-mempalace`. This is the cost of the shared emitter. The dependency is bundle-local (all modules install together) and narrow (only `event_emitter.py` is imported).