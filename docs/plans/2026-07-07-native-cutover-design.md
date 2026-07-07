# Native Cutover — Memory on amplifier-data, Zero MemPalace

**Date:** 2026-07-07
**Status:** Implementation-ready design (no code in this change)
**Supersedes:** `docs/plans/2026-07-07-substrate-adapter-completion-design.md`
(the shadow-adapter design — the shadow is now promoted to the ONE store)
**Scope:** This repo (`amplifier-bundle-memory`) implements everything below except
§10 (ripple), which specifies the exact lockstep changes other repos implement.
**Constellation edge:** `memory → amplifier-data` remains the ONLY intra-four edge.
The edge changes character: from optional shadow to hard dependency.

---

## 0. Key decisions (read this first)

| # | Decision | Choice | Rationale (full text in cited §) |
| --- | --- | --- | --- |
| D1 | Local embedder | **fastembed**, model `sentence-transformers/all-MiniLM-L6-v2` (ONNX, 384-dim) | Same vector space mempalace used → migration copies vectors; no torch; pip-installable; §4 |
| D2 | Where embedding runs | **Server-side, inside the memory daemon** | One resident model instance, stdlib-only clients, structurally-guaranteed single vector space (same property mempalace-mcp gave us); §4.2 |
| D3 | Multi-process architecture | **The existing gateway becomes the auto-started memory daemon** — ONE writer, spawn-on-first-use, pidfile/portfile discovery | DurableKernel is single-writer; the gateway already answers this; §5 |
| D4 | Search parity | Cosine top-k via `query_vector` + cheap pure-Python lexical bonus, computed daemon-side; **lexical-only degraded mode** when embedder unavailable | Simple, deterministic, never breaks the session; quality tradeoff documented; §6 |
| D5 | Terminology | **Keep wing/room/drawer/diary/garden/mine** (neutral organizational vocabulary, already mapped to substrate scopes); purge only the words "palace"/"mempalace" | The vocabulary is the product's domain language, not vendor branding; §7 |
| D6 | Tool name | `palace` → **`memory`**; the external SQLite `tool-memory` module is **dropped from the behavior** (name collision + duplicative post-cutover) | §7.3 |
| D7 | Migration | One-shot `amplifier-memory-import`: reads the ChromaDB palace directly (chromadb as `[migrate]` extra, read-only), **copies vectors** (same MiniLM space), writes through the daemon; idempotent | §9 |
| D8 | amplifier-data dependency | `[substrate]` extra becomes a **hard dependency** of tool-memory, pinned full SHA | It IS the store now; §8 |
| D9 | LLM judge (interject) | **Keep as-is**: opt-in, default false | Already privacy-clean by default; deleting it is scope creep for a cutover. The DEFAULT stack makes zero external network calls |
| D10 | Durability | The daemon **requires the Rust kernel** (durable `path=`); refuses to run a production store in-memory. `--ephemeral` flag exists for tests/DTU only | Silent memory loss is worse than a loud install prerequisite; §5.6 |
| D11 | `Transaction` adoption | Still deferred (unchanged from the adapter design) | Every multi-write flow fits one `WriteBatch` |
| D12 | Implementation order | Renames come **last** (B3), after native functionality is proven under old names (B1, B2) | Each pass stays reviewable; ripple repos break only at the mechanical rename, with lockstep specs ready |

---

## 1. Problem statement

Today (post `7b244a9`) the memory bundle is **two stores wearing one trench
coat**:

1. **mempalace ≥3.5.0 (PyPI, ChromaDB-backed) is the primary.** Every read and
   every write funnels through `_call_mcp_tool()` — a fresh `mempalace-mcp`
   subprocess per call, speaking JSON-RPC 2.0 over stdio
   (`modules/tool-mempalace/.../scripts/memory_store.py:42-141`). Six call
   sites across the tool and three hooks (each hook also carries a defensive
   private copy of the helper).
2. **`AmplifierDataMemoryStore` is a config-gated shadow** — complete
   (drawers, scopes, KG facts, vectors, diaries, atomic batches, three
   backends) but write-only + verify-only, off by default.

The costs of the split:

- **A vendor dependency owns the source of truth.** mempalace's store layout,
  embedding model, and MCP wire format are all upstream decisions we've
  already been burned by three times (the `mempalace mcp --call` phantom CLI,
  the wrong-store interject bug, the ToolResult contract mismatch — see
  HANDOFF.md 2026-07-07 entries).
- **A subprocess per memory operation.** Every search/remember/kg/diary op
  pays a full process spawn + ONNX model load inside `mempalace-mcp`.
- **Two stores to keep consistent** — the entire dual-write/compare apparatus
  (`DualWriteMemoryStore`, `dualwrite_compare.py`, shadow config blocks in
  two modules) exists only to manage the split.
- **The substrate is ready.** amplifier-data at `42c193b` delivers everything
  memory needs (CONSUMER_INTEGRATION §6: every row "Strong" or "Now
  supported"): E1 verbatim cells, scoped lenses, temporal KG with validity
  windows, scoped vectors, `WriteBatch` atomicity, `Embedder`/`VectorBackend`
  protocols, and a single-writer story.

**Goal:** amplifier-bundle-memory with ZERO mempalace/vendor references — no
mempalace PyPI dep, no `mempalace-mcp` subprocess, no `~/.mempalace` paths, no
"mempalace"/"palace" branding in module names, tool name, behaviors, docs, or
events. amplifier-data is the ONE store for reads AND writes. Everything
provable end-to-end in a DTU where mempalace is never installed.

**What we are NOT rebuilding:** the seam code shipped this week IS the core.
`AmplifierDataMemoryStore` becomes the store, the gateway becomes the daemon,
`GatewayClient` becomes the client every hook and tool op uses. The only
genuinely new machinery is (a) the local embedder, (b) daemon auto-start
lifecycle, (c) native read paths for search/status/traverse/diary-read/garden/
mine, (d) the migration script.

---

## 2. Architecture

```
  Amplifier session A          Amplifier session B          curate.dot / import
  ┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
  │ tool `memory`    │         │ tool `memory`    │         │ write_cells node │
  │ hooks: capture,  │         │ hooks: capture,  │         │ amplifier-memory-│
  │ briefing,        │         │ briefing,        │         │ import (one-shot)│
  │ interject,       │         │ interject, ...   │         │                  │
  │ behavioral-write │         │                  │         │                  │
  └────────┬─────────┘         └────────┬─────────┘         └────────┬─────────┘
           │  MemoryClient (stdlib urllib; ensure_daemon() auto-start)│
           │  POST /mcp {tool, arguments}  bearer/localhost auth      │
           ▼                             ▼                            ▼
  ┌───────────────────────────────────────────────────────────────────────────┐
  │            memory-daemon  (ONE process, ONE writer — was the gateway)     │
  │  ┌─────────────────────┐   ┌────────────────────────────────────────────┐ │
  │  │ FastEmbedEmbedder   │   │ NativeMemoryStore (was AmplifierDataMemory │ │
  │  │ all-MiniLM-L6-v2    │──▶│ Store): file / search / kg / diary /       │ │
  │  │ (lazy warm-load;    │   │ traverse / status — WriteBatch-atomic      │ │
  │  │ lexical fallback)   │   └───────────────────┬────────────────────────┘ │
  │  └─────────────────────┘                       │                          │
  │            discovery: ~/.amplifier/memory/daemon.json (+ lock, token)     │
  └────────────────────────────────────────────────┼──────────────────────────┘
                                                   ▼
                              AmplifierStore(path=~/.amplifier/memory/store.log)
                              DurableKernel (Rust) — append-only log = truth
```

Properties:

- **Single writer by construction.** Only the daemon ever opens the store
  file. Clients (sessions, hooks, importer, pipeline nodes) are stdlib-HTTP.
- **Single vector space by construction.** Only the daemon embeds — writes
  and queries share one model instance, the exact structural guarantee the
  2026-07-07 interject fix bought us with `mempalace_search`.
- **No new architecture.** Every box above exists today; boxes change names
  and the mempalace box disappears.

---

## 3. File & module plan

### 3.1 Rename table (every artifact, old → new)

Module directories, Python packages, entry points (B3; `git mv`, history
preserved):

| Old | New |
| --- | --- |
| `modules/tool-mempalace/` | `modules/tool-memory/` |
| `amplifier_module_tool_mempalace` (pkg) | `amplifier_module_tool_memory` |
| pyproject name `amplifier-module-tool-mempalace` | `amplifier-module-tool-memory` |
| entry point `tool-mempalace = ...:mount` | `tool-memory = amplifier_module_tool_memory:mount` |
| `modules/hooks-mempalace-capture/` | `modules/hooks-memory-capture/` |
| `amplifier_module_hooks_mempalace_capture` | `amplifier_module_hooks_memory_capture` |
| `modules/hooks-mempalace-briefing/` | `modules/hooks-memory-briefing/` |
| `amplifier_module_hooks_mempalace_briefing` | `amplifier_module_hooks_memory_briefing` |
| `modules/hooks-mempalace-interject/` | `modules/hooks-memory-interject/` |
| `amplifier_module_hooks_mempalace_interject` | `amplifier_module_hooks_memory_interject` |

Tool, classes, and behavior surface:

| Old | New |
| --- | --- |
| tool name `palace` | tool name `memory` (operations unchanged: search/remember/status/kg/traverse/diary/mine/events/garden) |
| `PalaceTool` | `MemoryTool` |
| `MempalaceCaptureHook` | `MemoryCaptureHook` |
| `MempalaceInterjectHook` | `MemoryInterjectHook` |
| `behaviors/mempalace.yaml` | `behaviors/memory.yaml` |
| `bundle.md` include `memory:behaviors/mempalace` | `memory:behaviors/memory` |
| `skills/mempalace/SKILL.md` | `skills/memory/SKILL.md` (content rewritten: `memory(operation=...)` examples) |
| config key `palace_path: "~/.mempalace"` | `home: "~/.amplifier/memory"` |
| config block `shadow_gateway: {...}` (both modules) | **deleted** (there is no shadow; daemon settings live under `daemon:` — §5.5) |
| external module `tool-memory` (SQLite fact store, git+…/amplifier-module-tool-memory) | **removed from behaviors/memory.yaml** (D6: name collision with the renamed module; the native KG covers explicit facts). Noted in CHANGELOG as breaking, with the `kg` op as the replacement |

Console scripts:

| Old | New |
| --- | --- |
| `mempalace-amplifier-data-gateway` | `memory-daemon` |
| `mempalace-load-captures` | `memory-load-captures` |
| `mempalace-write-cells` | `memory-write-cells` |
| `mempalace-dualwrite-compare` | **retired** (verification folds into `amplifier-memory-import --verify`, §9.4) |
| `mempalace-server-concurrency-check` | `memory-daemon-concurrency-check` |
| — (new) | `amplifier-memory-import` |

Paths and files on disk:

| Old | New |
| --- | --- |
| `~/.mempalace/palace` (ChromaDB) | `~/.amplifier/memory/store.log` (amplifier-data durable log) |
| `~/.mempalace/events/{sid}.jsonl` | `~/.amplifier/memory/events/{sid}.jsonl` |
| `~/.mempalace/spool/{sid}/` | `~/.amplifier/memory/spool/{sid}/` |
| `~/.amplifier/amplifier-data-token` | `~/.amplifier/memory/token` |
| — (new) | `~/.amplifier/memory/daemon.json`, `daemon.lock`, `daemon.log` |
| — (new) | `AMPLIFIER_MEMORY_HOME` env var overrides the home dir (tests/DTU) |

Event vocabulary (§7.2 has the full encoding): emitter `hook` values
`mempalace-capture` → `memory-capture`, `mempalace-interject` →
`memory-interject`, `mempalace-briefing` → `memory-briefing`,
`tool-mempalace` → `tool-memory`; coordinator-bridge event prefix
`memory-mempalace:` → `memory:`.

Docs: `README.md`, `context/instructions.md`, `context/memory-manifest.yaml`
(comments), `agents/{archivist,curator,docent}.md` (all `palace(...)` call
examples → `memory(...)`), `docs/development/dtu.md`, DTU profiles
(`.amplifier/digital-twin-universe/profiles/*.yaml`). `AGENTS.md` "Exposes"
line updates to `tool-memory`, `hooks-memory-capture`.

Explicitly NOT renamed / NOT purged: `project-context/*` (PROVENANCE, HANDOFF,
EXPERIMENT_JOURNAL, GLOSSARY are historical logs — append new entries, never
rewrite), `docs/plans/*` (design history), `CHANGELOG.md`, and the migration
module itself (which must say "mempalace" to describe what it imports).

### 3.2 New / rewritten files (signatures)

**`modules/tool-memory/amplifier_module_tool_memory/store.py`**
(was `scripts/memory_store.py`; moves up out of `scripts/` — it is the core,
not a pipeline helper)

```python
class MemoryStore(Protocol):            # unchanged contract
    def file(self, *, wing, room, content, source="", category=None,
             importance=None, embedding=None) -> Any: ...

class RecordingMemoryStore: ...          # unchanged (tests / dry-runs)

class NativeMemoryStore:                 # was AmplifierDataMemoryStore — same body
    """The ONE store. Backends: direct AmplifierStore (daemon-internal, tests)
    or MemoryClient (every other process). All existing methods carry over
    verbatim: file, search_vectors, assert_kg/invalidate_kg/query_kg/
    kg_timeline, file_diary, update_importance, rollback, _supports_atomic_update."""
    # NEW read surfaces (daemon-internal; exposed to clients via daemon tools):
    def search(self, query_vector, k, *, wing=None, room=None,
               lexical_query=None) -> list[dict]:
        """Hybrid rank (§6). Returns [{ref, score, content, wing, room,
        category, source}] — payloads regenerated server-side."""
    def list_drawers(self, *, wing=None, room=None, limit=200) -> list[dict]:
        """Scoped drawer listing for garden/status (scope graph_neighbors →
        regenerate, record_access=False)."""
    def read_diary(self, *, agent_name, last_n=10) -> list[dict]:
        """Cells under scope agent:{name}, SeqPos-ordered, newest last."""
    def status(self) -> dict:
        """{drawers, wings: [...], kg_facts, embedder: {...}, durable, path}."""
# DELETED: PalaceMemoryStore, DualWriteMemoryStore, _call_mcp_tool,
#          _MCP_PROTOCOL_VERSION.
```

**`modules/tool-memory/amplifier_module_tool_memory/embedder.py`** (new, §4)

```python
class FastEmbedEmbedder:
    """amplifier_data.embedding.Embedder implementation (embed(text) -> Sequence[float]).
    Lazy model load; thread-safe; loud-but-graceful when offline."""
    def __init__(self, model_name: str = DEFAULT_MODEL) -> None: ...
    def warm(self) -> None: ...          # background warm-load; sets .ready
    @property
    def ready(self) -> bool: ...
    @property
    def failed(self) -> str | None: ...  # reason string when load failed
    def embed(self, text: str) -> list[float]: ...  # raises EmbedderUnavailable if not ready

def lexical_score(query: str, text: str) -> float:
    """Deterministic pure-Python token-overlap score in [0,1] (§6.2)."""

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"   # 384-dim; == mempalace's space
```

**`modules/tool-memory/amplifier_module_tool_memory/daemon.py`**
(was `scripts/amplifier_data_gateway.py`; keeps `make_gateway`-style structure)

```python
def make_daemon(store, embedder, host, port, *, token,
                allow_localhost_bypass=True, version=__version__) -> ThreadingHTTPServer:
    """Existing dispatch tools carry over verbatim: write_cell, scope,
    assert_fact, invalidate_fact, regenerate, graph_neighbors, query_facts,
    add_embedding, query_vector, batch.
    NEW dispatch tools (§5.4): remember, search, status, kg_query, kg_timeline,
    kg_stats, traverse, diary_write, diary_read, list_drawers, shutdown.
    GET /health returns {"ok": true, "service": "memory-daemon",
    "version": ..., "embedder": {"ready": bool, "failed": str|None}, "durable": bool}."""

def run_daemon(*, home: Path, host="127.0.0.1", port=0, ephemeral=False,
               embedder_model=DEFAULT_MODEL, token_path=None) -> int:
    """Open durable store at home/store.log (REQUIRES Rust kernel unless
    --ephemeral, §5.6), start embedder warm-load thread, write daemon.json
    atomically, serve. SIGTERM/'shutdown' tool → close store, remove
    daemon.json, exit 0."""
```

**`modules/tool-memory/amplifier_module_tool_memory/client.py`** (new; the ONE
seam every hook/tool/pipeline node uses; stdlib-only — no amplifier-data, no
fastembed in session processes)

```python
class MemoryClient:                      # absorbs GatewayClient wholesale
    """All GatewayClient methods carry over (write_cell/scope/assert_fact/
    invalidate_fact/regenerate/graph_neighbors/query_facts/add_embedding/
    query_vector/write_batch+GatewayWriteBatch) PLUS domain calls mirroring
    the new daemon tools: remember(...), search(...), status(),
    kg_query(...), kg_timeline(...), kg_stats(), traverse(...),
    diary_write(...), diary_read(...)."""

def ensure_daemon(home: Path | None = None) -> MemoryClient | None:
    """Discovery → health-check → (re)spawn per §5.2. Returns None only when
    spawn is impossible; callers degrade loudly (emit memory:daemon_unavailable),
    NEVER raise into the session."""
```

**`modules/tool-memory/amplifier_module_tool_memory/migrate.py`** (new, §9) —
console script `amplifier-memory-import`.

**Rewired in place (no new files):** `__init__.py` (MemoryTool ops → client),
`garden.py` (drawer listing + embeddings via client; clustering math
unchanged), `event_emitter.py` (home dir + creation semantics, §7.2),
capture/briefing/interject/behavioral-write hook `__init__.py`s (transport
swap; gating/formatting/spool logic unchanged), `scripts/load_captures.py` /
`scripts/write_cells.py` (store construction → `NativeMemoryStore` via
client), `pipelines/curate.dot` (entry-point names).

**Deleted files:** `scripts/dualwrite_compare.py`,
`tests/test_dualwrite_compare.py`, `hooks-*-interject/tests/test_store_alignment.py`,
all three private `_call_mcp_tool_impl` fallback copies, shadow tests
(`test_shadow_gateway.py`, `test_tool_shadow_ops.py` — superseded by native
tests below).

---

## 4. Embedder spec (D1, D2)

### 4.1 Choice: fastembed, all-MiniLM-L6-v2 ONNX

- **fastembed** (Qdrant): ONNX Runtime under the hood, no torch, pip
  wheel, model auto-download from HF Hub to a local cache on first use.
  Pinned model: `sentence-transformers/all-MiniLM-L6-v2` (384-dim) — the
  same model family mempalace 3.5.0 embedded with server-side, so migrated
  vectors and freshly embedded queries live in one space (D7 copies vectors).
- **Rejected — onnxruntime + tokenizers direct:** ~150 lines of
  tokenization/pooling/normalization we'd own forever, plus a model
  distribution problem fastembed already solves. Fails the "does complexity
  add proportional value" test.
- **Rejected — chromadb's embed util without the chroma store:** drags the
  entire chromadb dependency into the runtime stack we are cutting over
  *away from*. chromadb appears only in the `[migrate]` extra.

### 4.2 Placement: daemon-side only

Embedding happens **only inside the memory daemon** (D2):

- `remember` (and capture-hook filing) sends TEXT; the daemon embeds and
  stages cell + scopes + facts + embedding on ONE `WriteBatch`.
- `search` sends TEXT; the daemon embeds the query with the same resident
  model → `query_vector(scope=…)`.
- Consequences: one ONNX model resident per machine (not per session);
  hooks/tool stay stdlib-only; write-space == query-space structurally.

### 4.3 Lifecycle & degradation

- **Never on the session hot path.** The capture hook's `tool:post` handler
  is untouched (gate → spool → queue). Embedding cost lands in the daemon,
  reached from the capture hook's existing background drain thread —
  `_process_job` swaps `_mcp_add_drawer` for `client.remember(...)`.
- **Lazy, non-blocking warm-load.** `run_daemon` starts serving immediately
  and kicks `embedder.warm()` on a background thread (first run downloads the
  model). Requests arriving before ready: `remember` files the drawer
  WITHOUT an embedding and records a `needs_embedding` fact
  (`assert_fact(ref, "needs_embedding", write_cell(b"true"))`); when the
  embedder becomes ready, a daemon-internal sweep embeds pending cells and
  invalidates the fact. `search` answers lexical-only with
  `degraded: "lexical_only"` in the response.
- **Loud-but-graceful offline.** If warm-load fails (no network, corrupted
  cache), the daemon stays up: `/health` reports
  `embedder: {ready: false, failed: "<reason>"}`, every degraded search
  response carries the flag, and clients emit `memory:embedder_unavailable`
  ONCE per session. The session never breaks (KG-N3).
- **Swappable (protocol seam).** The daemon accepts
  `--embedder-model <name>` and `--embedder none` (lexical-only by policy).
  `FastEmbedEmbedder` satisfies `amplifier_data.embedding.Embedder`, so a
  custom embedder is a drop-in daemon-side replacement. **The default is
  fully local — nothing leaves the machine, period.** The only network I/O
  the default stack can ever do is the one-time HF model download, and its
  absence degrades rather than breaks. (Interject's opt-in LLM judge stays
  as the documented, default-off exception — D9.)

---

## 5. Memory daemon: lifecycle spec (D3)

### 5.1 Files under `~/.amplifier/memory/` (override: `AMPLIFIER_MEMORY_HOME`)

| File | Purpose |
| --- | --- |
| `store.log` | the durable amplifier-data log — the ONE store |
| `daemon.json` | discovery: `{url, port, pid, version, token_file, started_at}` — written atomically (tmp + `os.replace`) by the daemon itself once listening |
| `daemon.lock` | spawn mutex (`O_CREAT\|O_EXCL`); §5.2 |
| `token` | bearer token, 0600, auto-generated (existing `ensure_token` moves here) |
| `daemon.log` | daemon stderr (rotation out of scope) |
| `events/`, `spool/` | JSONL event logs; capture spool (moved from `~/.mempalace/`) |

### 5.2 `ensure_daemon()` — discovery, spawn, recovery

```
1. Read daemon.json. If present:
   a. GET {url}/health (timeout 1s).
   b. healthy AND version == client's package version → return MemoryClient.
   c. healthy but version mismatch → POST shutdown (localhost-auth), wait ≤5s
      for exit, fall through to spawn.          # upgrade path
   d. unhealthy → if pid not alive, treat stale; if pid alive but health
      fails 3× over 3s, treat stale (wedged daemon: SIGTERM it, wait 2s).
2. Spawn race: open daemon.lock with O_CREAT|O_EXCL.
   - Winner: subprocess.Popen(["memory-daemon", "--home", home],
     start_new_session=True, stdout/stderr → daemon.log); poll daemon.json +
     /health up to 10s; remove daemon.lock; emit memory:daemon_spawned.
   - Loser: poll daemon.json + /health up to 10s (the winner is starting it).
   - Stale lock: if daemon.lock mtime > 30s and no healthy daemon, delete and retry once.
3. Any step fails → remove our lock if held, emit memory:daemon_unavailable
   (once per process), return None. Callers degrade (§5.7) — never raise.
```

- **Crash recovery (KG-N6):** `kill -9` leaves a stale `daemon.json`; step 1d
  detects (pid dead), step 2 respawns. `DurableKernel`'s torn-tail recovery
  handles a mid-append crash — the log is truth.
- **Upgrade (version mismatch):** step 1c. The daemon's `shutdown` tool
  finishes in-flight requests (it runs under the same dispatch path), closes
  the store, removes `daemon.json`, exits 0.
- **Idle shutdown:** NOT implemented (dead complexity — the daemon is a
  stdlib HTTP server plus one ONNX model; RSS ≈150 MB). Recorded as a
  possible future `--idle-exit` flag, nothing more.
- **No user ceremony:** the first memory operation of the first session
  spawns the daemon; every later session discovers it.

### 5.3 Concurrency & auth (unchanged mechanisms)

- Global write lock serializes mutating dispatch (as today). Embedding runs
  OUTSIDE the lock (embed → then lock + write) so a slow embed never blocks
  readers.
- Auth: bearer token + socket-level localhost bypass + constant-time compare
  (verbatim from the gateway). `shutdown` requires localhost or token.

### 5.4 New dispatch tools (wire shapes)

| Tool | Arguments | Returns |
| --- | --- | --- |
| `remember` | `{wing, room, content, source?, category?, importance?}` | `{ref}` — embeds + files atomically (WriteBatch); `needs_embedding` path when embedder not ready |
| `search` | `{query, k, wing?, room?}` | `{results: [{ref, score, content, wing, room, category, source}], degraded: null\|"lexical_only"}` |
| `status` | `{}` | `{drawers, wings, kg_facts, embedder, durable, path, version}` |
| `kg_query` | `{subject?, predicate?}` | `{facts: [[s,p,o], ...]}` (anchor-resolved strings) |
| `kg_timeline` | `{subject}` | `{entries: [{seq_pos, op, predicate, object}]}` |
| `kg_stats` | `{}` | `{facts, entities}` |
| `traverse` | `{start, max_hops, rel_type?}` | `{refs: [...]}` (wraps `query_graph`; `start` accepts an entity string, anchored server-side) |
| `diary_write` | `{agent_name, entry, topic}` | `{ref}` (wraps `file_diary`) |
| `diary_read` | `{agent_name, last_n}` | `{entries: [{ref, entry, topic, seq_pos}]}` |
| `list_drawers` | `{wing?, room?, limit}` | `{drawers: [{ref, content, wing, room, category, importance}]}` (garden/status) |
| `shutdown` | `{}` | `{ok: true}` then graceful exit |

Existing generic tools (`write_cell` … `batch`) stay — the importer and tests
use them.

### 5.5 Daemon configuration

Flags with sane defaults; the behavior config's `daemon:` block on tool-memory
is passed through by whichever client spawns first (first spawner wins —
documented; there is exactly one daemon per home dir, so disagreement is a
config smell, not a runtime hazard). `AMPLIFIER_MEMORY_HOME` is the test/DTU
isolation knob.

### 5.6 Durability requirement (D10)

`run_daemon` refuses to open a production store without the Rust kernel:
if `amplifier_data.RUST_AVAILABLE` is false and `--ephemeral` was not passed,
it exits non-zero with a one-line remedy (installing amplifier-data from the
git pin builds the kernel via maturin; a Rust toolchain is the prerequisite —
see §8). `--ephemeral` (in-memory store) exists for tests and DTU smoke only
and stamps `"durable": false` into `/health` so nothing can mistake it for
production.

### 5.7 Client degradation contract

`ensure_daemon() → None` (or any transport failure) means, per caller:
capture drain job → `capture_failed` event, spool entry retained for replay
(existing contract, unchanged); briefing → skip section, event notes the
miss; interject → `interject_skipped(reason="daemon_unavailable")`;
tool ops → `ToolResult(success=False, error=...)` — loud, exactly like the
mempalace-missing failure mode proven in the 5/5 DTU run.

---

## 6. Search parity (D4)

### 6.1 What mempalace gave us / what we ship

mempalace's search was hybrid (bm25 + cosine) over ChromaDB. We ship,
daemon-side:

1. Embed query → `query_vector(vec, k*3, scope=wing_or_room_ref)` →
   candidate set with cosine scores.
2. Regenerate candidate payloads (`record_access=False`) and fold in a
   lexical bonus: `final = 0.85 * cosine + 0.15 * lexical_score(query, text)`;
   return top-k.
3. `lexical_score` = deterministic token-set overlap
   (`|q ∩ d| / max(1, |q|)`, lowercased, `\w+` tokens) — ~10 lines, stdlib.

**Documented quality tradeoff:** the lexical term only re-ranks vector
candidates; a document with zero semantic similarity but exact keyword
overlap can be missed (true BM25 over the full corpus would catch it). At
memory's scale (thousands of drawers) and given briefing/interject read
patterns, this is acceptable; revisit behind the `VectorBackend` stud if
recall benchmarks regress (the falsification harness's recall@5 gate is the
tripwire).

### 6.2 Embedder-less degraded mode

When the embedder is not ready/failed: lexical-only scan over drawers in
scope (`list_drawers` internals: scope → members → regenerate → score →
top-k), response flagged `degraded: "lexical_only"`, client emits
`memory:search_degraded` once per session. Slow for huge wings — acceptable
for a degraded mode, documented.

---

## 7. Vocabulary, events, and behavior surface

### 7.1 Terminology (D5)

Keep: **wing** (project/person scope), **room** (topic scope), **drawer**
(verbatim memory cell), **diary**, **garden**, **mine**. These map today to
substrate conventions and remain the tool's parameter names:

| Term | Substrate encoding (unchanged) |
| --- | --- |
| drawer | `write_cell(content)` |
| wing | scope cell `wing:{name}` + `scoped_to` edge |
| room | scope cell `room:{name}` + `scoped_to` edge |
| KG entity | anchor cell `entity:{name}` |
| diary entry | cell + scopes `agent:{name}`, `room:{topic}` + `has_source = diary:{name}` |
| category/importance/source | `has_category` / `has_importance` / `has_source` facts |

Purged words: "palace", "mempalace" (per the rename table §3.1).

### 7.2 Event encodings

JSONL emitter (`event_emitter.py`):

- Directory: `~/.amplifier/memory/events/{session_id}.jsonl`.
- **Creation semantics change:** the old emitter no-op'd unless
  `~/.mempalace` existed (mempalace-init was the signal). Natively, memory
  owns its home: the emitter (and spool) `mkdir -p` the home lazily. The
  "silent no-op when uninitialized" behavior is deleted — there is no init
  step anymore.
- Schema: same line shape (`ts, sid, hook, event, ok, preview, data, v`);
  **`v: 2`** (hook names changed — the additive-evolution field doing its job).
- `hook` values: `memory-capture`, `memory-briefing`, `memory-interject`,
  `tool-memory`, `memory-daemon`, `memory-import`.
- Event names carried over unchanged: `capture_queued/skipped/overflowed`,
  `drawer_filed`, `capture_failed`, `replay_enqueued`, `briefing_assembled`,
  `memory_surfaced`, `interject_skipped`, `garden_completed/progress`,
  `kg_filed` (was `kg_shadow_filed` — no longer a shadow), `diary_filed`.
- New events: `daemon_spawned`, `daemon_respawned`, `daemon_unavailable`,
  `embedder_ready`, `embedder_unavailable`, `search_degraded`,
  `import_completed`.
- Coordinator-bridge topics: `memory-mempalace:X` → `memory:X` for every X
  (drawer_filed, capture_failed, garden_completed, garden_progress,
  memory_surfaced, interject_skipped, briefing_assembled). Consumers:
  hooks-behavioral-write (in-repo, updated in B2) and the interject
  briefing-listener.

### 7.3 `behaviors/memory.yaml` (replaces mempalace.yaml)

Same module lineup minus retirements; config deltas:

```yaml
tools:
  - module: tool-memory
    source: ../modules/tool-memory
    config:
      home: "~/.amplifier/memory"        # was palace_path
      garden_max_drawers: 200
      daemon:                            # first spawner's config wins (documented)
        auto_start: true
        embedder_model: "sentence-transformers/all-MiniLM-L6-v2"
        # embedder_model: "none"  → lexical-only by policy
  # tool-memory (external SQLite fact store): REMOVED (D6)
hooks:
  - module: hooks-memory-capture        # config unchanged minus shadow_gateway
  - module: hooks-memory-briefing       # config unchanged
  - module: hooks-project-context       # unchanged
  - module: hooks-memory-interject      # config unchanged (llm_judge_enabled stays, default false)
  - module: hooks-project-isolation     # unchanged
```

`bundle.md`: `- bundle: memory:behaviors/mempalace` → `memory:behaviors/memory`;
bundle version → 2.0.0.

---

## 8. Dependencies (D8)

`modules/tool-memory/pyproject.toml`:

```toml
dependencies = [
    "amplifier-core>=0.1.0",
    # THE store (was the [substrate] extra). Full SHA, never @main.
    # PIN COUPLING: conductor's dep-amplifier-data must move in lockstep (§10.2)
    # until it is retired.
    "amplifier-data @ git+https://github.com/michaeljabbour/amplifier-data@<HEAD-SHA-at-impl-time>",
    "fastembed>=0.3",
]
[project.optional-dependencies]
migrate = ["chromadb>=0.5"]     # read-only import of a legacy palace (§9)
# [substrate] extra: DELETED.  mempalace dependency: DELETED (all modules).
```

Hooks' pyprojects: `mempalace>=3.5.0` deleted;
`amplifier-module-tool-mempalace>=1.0.0` → `amplifier-module-tool-memory>=2.0.0`.
All module versions → **2.0.0** (breaking rename + transport change).

**Install-experience note (honest constraint):** amplifier-data's build
backend is maturin; installing the git pin builds the Rust kernel, so a Rust
toolchain becomes an install prerequisite for the memory bundle (durability
requires the kernel anyway — D10). Mitigation is upstream: amplifier-data
publishing abi3 wheels (its `_rust.abi3.so` is already abi3). Recorded as a
ripple ask (§10.4), NOT a blocker.

---

## 9. Migration spec (D7): `amplifier-memory-import`

One-shot, read-only against mempalace's on-disk palace; writes through the
daemon (single-writer preserved).

1. **Preconditions:** `pip install 'amplifier-module-tool-memory[migrate]'`;
   source defaults to `~/.mempalace/palace` (flag `--source`). The palace is
   opened via `chromadb.PersistentClient(path=source)` → collection
   `mempalace_drawers` (verified layout of mempalace 3.5.0), read-only —
   **`~/.mempalace` is never modified.**
2. **Drawers + embeddings:** page through
   `collection.get(include=["documents","metadatas","embeddings"], limit=500, offset=…)`.
   Per record: batch write via `MemoryClient` — cell = document bytes;
   wing/room scopes from metadata (`wing`, `room`); `has_source` ←
   `source_file`; `has_category` ← `category` (if present);
   `has_importance` ← `importance` (if present); **embedding = the stored
   vector, copied verbatim** (`add_embedding` staged in the same batch) —
   same MiniLM space as the native embedder (D1), so no re-embed.
   `--re-embed` flag re-embeds through the daemon instead (for users
   switching models).
3. **KG + diaries (best-effort, honest):** mempalace 3.5.0 persists KG
   triples and diaries outside the chroma collection; the implementer MUST
   verify the exact on-disk format against the installed package source in
   the `[migrate]` venv at implementation time. If found: triples →
   `assert_kg`, diaries → `diary_write`. If the format is absent or
   unrecognized: **report `skipped: {kg: reason, diaries: reason}` loudly**
   — never silently drop a kind, never guess a format.
4. **Idempotency + verify:** content addressing dedups cells/scopes/embeddings
   on re-run (same bytes → same ref). Facts get a read-before-write guard
   (`query_facts(subject=ref, predicate=p)` contains the value → skip).
   `--verify` re-reads every imported drawer through `search`/`regenerate`
   and byte-compares (this absorbs the retired `dualwrite_compare` role).
5. **Report (stdout JSON):**
   `{drawers, embeddings_copied, kg_facts, diaries, skipped, errors, verified}`;
   exit 0 iff `errors == 0`. Emits `memory-import: import_completed`.

---

## 10. Ripple (lockstep specs — other repos implement; NOT this repo)

### 10.1 Cohort detection ("a tool named palace")

- `~/dev/amplifier-bundle-context-intelligence-survey/modules/hooks-survey-capture/amplifier_module_hooks_survey_capture/cohort.py`
  — the source of truth. Change:

  ```python
  _MEMORY_TOOLS = ("memory", "palace")   # was _MEMORY_TOOL = "palace"; keep
                                         # "palace" for back-compat with old sessions
  "memory": any(_has_tool(coordinator, t) for t in _MEMORY_TOOLS),
  ```

- `~/dev/amplifier-bundle-behavioral-plasticity/modules/tool-falsification-harness/.../cohort.py`
  — its FALLBACK detector mirrors the same change (`_MEMORY_TOOL = "palace"`
  → `_MEMORY_TOOLS` tuple, same semantics); its primary path already defers
  to survey. Tests in both repos pin both names.
  **Ordering:** these are safe to land BEFORE memory's B3 (detecting a
  not-yet-existing name is harmless), so ship them first.

### 10.2 Conductor (`amplifier-bundle-behavioral-plasticity`)

- `bundle.md`: `memory:behaviors/mempalace` → `memory:behaviors/memory`.
- `modules/dep-amplifier-data`: **retire it** — tool-memory now hard-pins
  amplifier-data (§8), so the carrier module is redundant; the conductor
  inherits the pin transitively. (If the conductor owner prefers keeping an
  explicit pin, it MUST equal tool-memory's SHA — the old lockstep rule.)
- `memory_backend.py` (falsification harness): `_TEST_FILE_CANDIDATES` gains
  `modules/tool-memory/...` alongside the old path; the
  `hooks-mempalace-briefing` import for `_rerank_by_importance` →
  `amplifier_module_hooks_memory_briefing`.

### 10.3 Memory's own DTU profiles (this repo, B3)

- `memory-native-e2e.yaml`: conductor compose with **mempalace ABSENT**
  (assert `pip show mempalace` fails in-container); Rust toolchain provisioned
  (or amplifier-data wheel via the DTU `wheel_from_git` pattern); run the
  friend-scenario: session 1 `memory remember` → session 2 (separate process)
  `memory search` recalls verbatim; cohort label == `memory` (or `ci+memory`);
  `falsification_harness` smoke green; `kill -9` the daemon mid-run →
  next op respawns (KG-N6 in vivo).
- `memory-migration-e2e.yaml`: seeds a real mempalace palace (mempalace
  installed ONLY to seed, then uninstalled), runs `amplifier-memory-import`,
  asserts KG-N5.

### 10.4 amplifier-data (ask, not a change we make)

Publish abi3 wheels so memory's hard dep doesn't require a Rust toolchain at
install time (§8). Until then the toolchain prerequisite is documented in
memory's README.

---

## 11. Phased implementation (3 implementer passes)

### B1 — Native core: embedder + daemon + client (old names, additive)

Land in `amplifier_module_tool_mempalace` (renames come in B3): `embedder.py`,
daemon upgrades (§5.4 tools, `/health` version+embedder, `shutdown`,
`daemon.json` write, `run_daemon` durability gate), `client.py`
(`MemoryClient` + `ensure_daemon`), new store read surfaces
(`search`/`list_drawers`/`read_diary`/`status` on the seam class). No
existing behavior changes; mempalace paths untouched.

**B1 gates:** existing suites regression-green (tool 220, capture, briefing,
interject); NEW tests green in a substrate venv: daemon domain-tool
round-trips; **KG-N2** (two OS processes writing via `ensure_daemon`
concurrently, no corruption, both readable); **KG-N3** (embedder forced-fail
→ remember still files + `needs_embedding`, search lexical hit + degraded
flag + event); **KG-N6** (`kill -9`, next call respawns and succeeds);
version-mismatch respawn; `python_check` clean.

### B2 — Cutover: tool + hooks rewired native; mempalace deleted

MemoryTool ops → `MemoryClient` (search/remember/status/kg/traverse/diary
per §5.4; `mine` → minimal native file-walker filing via `remember` — files
mode walks md/txt/code files and chunks them; convos mode parses conversation
exports the same way it does today, minus the subprocess); `garden.py` →
`list_drawers` + vectors via daemon (clustering math unchanged); capture
`_process_job` → `client.remember`; briefing → `search`/`kg_query`/
`diary_read`; interject `_mcp_search` → `client.search`; behavioral-write
event names + imports. DELETE: `_call_mcp_tool` + 3 private copies,
`PalaceMemoryStore`, `DualWriteMemoryStore`, `dualwrite_compare`, shadow
config/wiring/tests, `test_store_alignment.py`, mempalace deps from all
pyprojects; `[substrate]` extra → hard dep (§8). The 30 contract tests
(`test_palace_tool_contract.py`) convert transport (success AND failure
branches — failure now = daemon unavailable) but keep the
one-positional-dict orchestrator calling convention pins.

**B2 gates:** ALL per-module suites green in a venv where **mempalace is not
installed** (conftest asserts `importlib.util.find_spec("mempalace") is
None`); **KG-N1** (real `execute({"operation": "remember", ...})` →
separate-process `execute({"operation": "search", ...})` recall round-trip
through the daemon with the real embedder); `grep -ri "mempalace-mcp"
modules/` returns nothing; `python_check` clean.

### B3 — Renames + migration + docs + ripple handoff

The §3.1 rename sweep (`git mv`), event schema v2 + home-dir move,
`behaviors/memory.yaml` + `bundle.md`, skills/agents/README/instructions
rewrite, `migrate.py` + `[migrate]` extra, DTU profiles (§10.3), CHANGELOG
(2.0.0, breaking: tool name, paths, external tool-memory removal, Rust
prerequisite), PROVENANCE **appended** D-entry "Native cutover — mempalace
retired" (+ one-line supersession notes under the adapter entries), HANDOFF
updated, ripple specs (§10.1/10.2) handed off loudly in HANDOFF + commit
message.

**B3 gates:** **KG-N4** (grep gate below); **KG-N5** (migration); full suite
green post-rename; DTU `memory-native-e2e` pass recorded (or profile+test
landed with an explicit "not yet run" in HANDOFF — never claim an unrun
smoke); the 30 contract tests still green under the new tool name.

---

## 12. Killer gates (numbered, executable)

| Gate | Assertion |
| --- | --- |
| **KG-N1** | Remember→search round-trip through the REAL tool surface: `MemoryTool.execute({"operation":"remember", "wing":"wing_e2e", "content": C})` returns `success=True` + a ref; a SECOND process's `execute({"operation":"search", "query": q})` returns C verbatim with score above threshold, via the auto-started daemon and the real local embedder. (Test: `modules/tool-memory/tests/test_native_roundtrip.py`; also the DTU friend-scenario.) |
| **KG-N2** | Cross-process single-writer: two `multiprocessing` (spawn) workers each `ensure_daemon()` + `remember` 50 distinct drawers concurrently → exactly one daemon pid observed by both; store contains all 100 drawers; every one regenerates byte-identically; `daemon.json` consistent. (Extends `memory-daemon-concurrency-check`.) |
| **KG-N3** | Embedder-offline degraded mode: force the embedder to fail loading → `remember` succeeds and marks `needs_embedding`; `search` returns a lexical hit for exact-keyword content with `degraded:"lexical_only"`; `memory:embedder_unavailable` emitted; NO exception reaches the hook/tool caller. When the embedder later becomes ready, the pending sweep attaches the embedding and the same query succeeds semantically. |
| **KG-N4** | Vendor-zero grep gate, encoded as a test: `grep -riE "mempalace|\.mempalace"` over the repo — excluding `.git`, venvs, `docs/plans/`, `project-context/`, `CHANGELOG.md`, and the migration module (`migrate.py`, its tests, the `[migrate]` extra lines) — returns **nothing**. Additionally `grep -ri "palace" modules/ behaviors/ skills/ context/ agents/ bundle.md README.md` returns nothing. |
| **KG-N5** | Migration: seed a throwaway chroma palace (fixture writes `mempalace_drawers` with documents+metadatas+embeddings — chromadb only, mempalace NOT required) → `amplifier-memory-import --source <tmp> --verify` exits 0, report counts match the seed, `memory search` finds migrated content semantically (copied vectors), AND the source tree is byte-unchanged (read-only proof). |
| **KG-N6** | Daemon lifecycle: `ensure_daemon()` → `kill -9 <pid>` → next `remember` respawns (new pid, `memory:daemon_respawned`), succeeds, and pre-crash drawers are still readable (durable log). Also: stale `daemon.json` with a dead pid AND a concurrent spawn race (two processes race `ensure_daemon` after the kill) yields exactly one new daemon. |

Carried-over gates: the KG-V/A/K/D/G suite from the adapter design remains
green throughout (the seam methods they pin are unchanged); the 30 tool
contract tests carry to the `memory` tool.

---

## 13. Definition of Done

- [ ] B1/B2/B3 landed in order, each with its gates green **executed, not
      skipped**, in a venv with amplifier-data (Rust kernel) installed.
- [ ] All per-module suites green with mempalace **absent** from the
      environment (conftest asserts it).
- [ ] KG-N1…KG-N6 encoded as tests and green; KG-N4 wired into CI.
- [ ] Zero mempalace: no PyPI dep, no subprocess, no `~/.mempalace` runtime
      path, no palace/mempalace branding outside history dirs + migrate.py.
- [ ] Daemon: auto-start, crash-respawn, version-mismatch respawn, single
      writer under concurrent sessions — all pinned by tests.
- [ ] Default stack makes zero external network calls except the one-time
      HF model download, whose absence degrades gracefully (KG-N3).
- [ ] `amplifier-memory-import` idempotent, read-only on the source, with
      `--verify`; KG-N5 green.
- [ ] `behaviors/memory.yaml` + `bundle.md` + skills/agents/docs renamed and
      truthful; module versions 2.0.0; CHANGELOG breaking-changes section.
- [ ] DTU profiles landed; `memory-native-e2e` run recorded in HANDOFF (or
      explicitly marked unrun).
- [ ] Ripple specs (§10.1, §10.2) flagged in HANDOFF + commit message with
      exact file/line targets; cohort-detection change confirmed shippable
      ahead of B3.
- [ ] PROVENANCE appended (never rewritten); HANDOFF current; `python_check`
      clean; no edits to `~/dev/amplifier-data` or the ripple repos from
      this repo's passes.

---

## 14. Explicitly out of scope

- **Ripple repo implementations** (survey, falsification-harness, conductor)
  — specified in §10, owned by those repos, landed in lockstep.
- **ANN backend** behind `VectorBackend` — brute-force cosine is fine at
  memory's scale; the stud exists when benchmarks demand it.
- **amplifier-data changes** of any kind (wheels are an ask, §10.4; the
  substrate API is consumed as-is at the pinned SHA).
- **Recall-quality work** beyond §6 parity (BM25-proper, re-ranking models);
  the falsification harness measures; a future design responds.
- **Idle-shutdown / daemon log rotation** — noted, deliberately not built.
- **`Transaction` adoption** (D11) and mutable re-embedding wiring
  (`embeddings.py` T1-MEM-4 stays off).
- **curate.dot / attractor semantics** — only entry-point names change.
- **Importance/plasticity logic** (`phase3.py`, `salience.py`, `usage.py`,
  hooks-behavioral-write semantics) — transport and event names only.
