# Architecture Decisions — memory-bundle

This document records the key architecture decisions made during the design and
implementation of the memory-bundle.  Each ADR captures the context, the decision,
and the rationale so that future contributors understand why the system is built
the way it is.

---

## ADR-001 — Two-layer memory architecture (palace + project-context)

**Status:** Accepted

**Context:**  
The memory-bundle must serve two distinct retrieval patterns: (1) fuzzy semantic recall
of fragments from past sessions (palace layer), and (2) structured, deterministic
retrieval of current-project state (project-context layer).  These patterns have
different latency budgets, different staleness tolerances, and different access
mechanisms.

**Decision:**  
We decided to implement a two-layer memory architecture.  The palace layer (MemPalace)
handles fuzzy semantic search via embedding-based recall and is the module's primary
design concern.  The project-context layer is a simple file tree that the briefing
hook reads verbatim; no embedding is computed.

**Rationale:**  
Mixing structured context documents into the embedding space would degrade recall
precision for semantic queries.  Keeping the two layers separate preserves the
design intent of each and makes the module easier to reason about.

---

## ADR-002 — Behaviour-based bundle install (`--app` with `#subdirectory=`)

**Status:** Accepted

**Context:**  
The DTU profile must install the memory-bundle inside the container.  Three approaches
were evaluated: (a) pip install from the repo root, (b) manual YAML copy, (c) Amplifier
behaviour flag with `--app` and `#subdirectory=`.

**Decision:**  
We decided on behaviour-based bundle install using:

```bash
amplifier bundle add --app git+https://gitea.local/memory-bundle.git#subdirectory=behaviors/mempalace.yaml
```

**Rationale:**  
The `#subdirectory=` fragment is an established pattern in the Amplifier ecosystem for
referencing a specific behaviour definition from a repository root.  It keeps the
container image lean (no transitive test dependencies are pulled in) and stays
in sync with the source repository without manual YAML maintenance.

---

## ADR-003 — Hot-path / drain-thread split for capture

**Status:** Accepted

**Context:**  
The capture hook fires on every `tool:post` event.  Embedding calls to the external
API take 100–400 ms, which is unacceptable latency on the hot path.

**Decision:**  
We decided to split capture into a synchronous hot-path component and an asynchronous
drain-thread component.  The hot-path emits `capture_queued` or `capture_skipped`
synchronously and writes the raw fragment to the spool directory.  The drain thread
reads from the spool, calls the embedding API, and writes to the palace.

**Rationale:**  
The pattern decouples API latency from tool response latency.  The spool directory
acts as a durable queue: if the process crashes after spool-write but before
palace-write, the fragment can be recovered on next startup.  The design also makes
the module easier to test: hot-path tests require no API keys; drain-thread tests
require real keys but run separately.

---

## ADR-004 — Dual-palace seeding for the DTU

**Status:** Accepted

**Context:**  
Integration tests require a palace that is pre-seeded with known fragments so that
recall precision can be asserted deterministically.  Re-running `mempalace mine`
inside each test is too slow (4–8 seconds per run) when real API keys are required.

**Decision:**  
We decided to implement a dual-palace seeding pattern for the DTU profile.  A seed
palace is built once at container-init time and frozen to `~/.mempalace-seed`.  A
3-line `reset-palace` script restores the working palace from the frozen snapshot
between test runs.

**Rationale:**  
The dual-palace pattern reduces per-test overhead from ~6 seconds to ~50 ms for the
palace restore step.  The frozen snapshot guarantees deterministic fragment content
across runs, which is critical for asserting recall precision thresholds.

---

## ADR-005 — Real API keys for end-to-end tests (< $0.10 / run)

**Status:** Accepted

**Context:**  
Early prototypes used a mock embedding client that returned random unit vectors.
Recall precision tests failed non-deterministically and the results were not meaningful
for validating the palace query logic.

**Decision:**  
We decided that real API keys for the embedding provider are required for all
integration and end-to-end tests.  Mock clients are only permissible in unit tests
that explicitly test client-error handling paths.

**Rationale:**  
Real embeddings cluster in meaningful semantic space.  Mock vectors do not.  The cost
of running the full integration suite against the seed-palace fixture corpus is less
than $0.10 per run, which is acceptable given the confidence gain.  The DTU profile
passes API keys in via environment variable so they are never committed to source.

---

## ADR-006 — Gitea mirror for bundle install

**Status:** Accepted

**Context:**  
The DTU container must install the memory-bundle from a Git URL.  Using the public
GitHub URL requires internet access from inside the container, which conflicts with
the design goal of fully isolated test environments.

**Decision:**  
We decided to run a local Gitea mirror inside the DTU network and configure the
bundle install URL to point to it.  The DTU profile YAML also sets
`allow_uv_github_fast_path: false` to prevent uv from bypassing the mirror by
resolving GitHub URLs directly.

**Rationale:**  
A Gitea mirror provides a stable, reproducible install source that does not depend
on public internet availability during CI runs.  The `allow_uv_github_fast_path: false`
setting is a necessary companion because uv's fast path would silently bypass the
mirror if left enabled, undermining the isolation guarantee.
