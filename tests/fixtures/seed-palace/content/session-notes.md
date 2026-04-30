# Session Notes — 2026-04-29

## Overview

This session focused on wiring up the end-to-end test infrastructure for the memory-bundle.
The goal was to get a Digital Twin Universe (DTU) environment that boots with a pre-seeded
MemPalace palace so integration tests can exercise the full capture→recall→briefing pipeline
without standing up external services.

---

## Decisions made this session

### Dual-palace pattern adopted

We decided to use a dual-palace seeding strategy for the DTU profile.  A "seed palace" is
frozen at container-init time (`cp -r ~/.mempalace ~/.mempalace-seed`) so individual test
suites can call `reset-palace` to restore a known-good baseline without re-running the full
`mempalace mine` pipeline.  This pattern was decided after observing that mine runs against
the fixture corpus take roughly 4–8 seconds due to real API embedding calls, which is
acceptable once at boot but not acceptable inside every test.

### Behaviour-based bundle install

We decided to install the memory-bundle inside the DTU container using Amplifier's behaviour
flag rather than a conventional pip dependency:

```bash
amplifier bundle add --app git+https://gitea.local/memory-bundle.git#subdirectory=behaviors/mempalace.yaml
```

The `#subdirectory=behaviors/mempalace.yaml` fragment tells the installer to pull only the
behaviour definition from the repository root, keeping the container image lean.  This
approach was decided after evaluating direct pip installs (too many transitive deps) and
manual YAML copies (fragile, diverges from source).

---

## Blockers resolved

### uv GitHub fast path bypassing Gitea proxy

The DTU profile uses a local Gitea mirror to serve bundle repositories without reaching the
public internet.  During initial bring-up we observed that `uv` was resolving GitHub URLs
directly, bypassing the mirror.  This was resolved by adding `allow_uv_github_fast_path: false`
to the DTU profile YAML, which forces uv to respect the `[[source]]` redirects configured
in `pyproject.toml`.

### Project-context discovery via `_find_project_context_dir`

The briefing hook locates its input documents by calling `_find_project_context_dir()`,
which walks upward from the current working directory checking each ancestor for a
subdirectory named `project-context/`.  During testing we observed that the hook was unable
to find the directory when tests were launched from `/workspace/tests/`.  This was resolved
by pre-copying `tests/fixtures/seed-palace/project-context/` to `/workspace/project-context/`
during the DTU init sequence so the walk terminates at `/workspace/`.

---

## Patterns observed

### Synchronous `*_queued` / `*_skipped` event emission

The capture hook emits `capture_queued` or `capture_skipped` events synchronously on the
hot path, before handing work off to a drain thread.  The actual embedding call and palace
write happen asynchronously in the drain thread (or subprocess for large payloads).  This
pattern was observed consistently across all three capture entry points (tool:post,
tool:pre, session:start) and is now documented as a convention: slow work is always
deferred; fast bookkeeping events are always synchronous.

---

## Lessons learned

### Spool directory must exist before `tool:post`

We learned the hard way that the spool directory (`/workspace/spool/`) must be created
before the first `tool:post` hook fires.  If the directory is missing the drain thread
silently drops the fragment rather than raising an error.  The DTU init sequence now
includes `mkdir -p /workspace/spool` as an explicit step.

### Real API keys are non-negotiable for integration tests

We learned that mock embedding clients produce vectors that cluster differently from real
embeddings, causing recall precision tests to fail with misleading results.  Real API keys
for the embedding provider are non-negotiable for integration tests.  The cost is less than
$0.10 per full test suite run against the seed corpus.

---

## Next steps

- Wire up the reset-palace helper script into the DTU profile
- Add `verify-seeding.sh` smoke test that asserts at least 3 fragments are recalled
- Extend the fixture corpus with a third content file covering hook contract edge-cases
- Confirm that `allow_uv_github_fast_path: false` propagates to nested sub-installs
