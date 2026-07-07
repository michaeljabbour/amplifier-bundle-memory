# Digital Twin Universe (DTU) — End-to-End Test Environment

This guide explains how to provision, use, and maintain the DTU environment for
`amplifier-bundle-memory`. The primary DTU profile is at:

```
.amplifier/digital-twin-universe/profiles/memory-native-e2e.yaml
```

A second profile validates the migration path from a legacy vendor store:

```
.amplifier/digital-twin-universe/profiles/memory-migration-e2e.yaml
```

---

## Why the DTU?

Unit tests for this bundle mock their dependencies to run fast and in isolation:

- `ensure_daemon` is patched so no real daemon subprocess is spawned.
- `emit_event` is replaced by an in-process spy.
- The memory store is shadowed by a temp directory or a stub object.

These stubs are valuable for regression safety, but they do not prove the bundle
works in a real Amplifier session against a real amplifier-data-backed daemon.
The DTU closes that gap.

Inside a DTU environment the following all run against real infrastructure:

- **Real bundle-install path.** `amplifier bundle add` fetches the bundle from a
  local Gitea mirror using the subdirectory syntax
  (`git+https://...@main#subdirectory=behaviors/memory.yaml`). Any packaging
  or manifest error that the mocks hide will surface here.
- **A real auto-started memory daemon with semantic search.** The daemon opens
  a durable amplifier-data store (Rust kernel, built at install time) and a
  real local fastembed embedder — no vendor dependency, no mocks.
- **Real Anthropic / OpenAI calls.** The LLM provider is not stubbed. Prompt
  regressions that do not break unit tests become visible.
- **Full event flow.** Every hook — briefing, post-tool, post-assistant — fires
  in sequence inside a genuine Amplifier session. Ordering bugs and missing
  awaits show up here.

Run unit tests first (they are fast), then run the DTU suite before opening a
pull request or shipping a release.

---

## Prerequisites

You need six things before you can launch the DTU:

1. **Incus** — the container runtime used by `amplifier-digital-twin`. On
   macOS this requires Colima as a Linux host VM; see the
   [`installing-incus` guide](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/blob/main/docs/installing-incus.md)
   for platform-specific steps.
2. **`amplifier-digital-twin` CLI** — install with `uv` (the package is not
   on PyPI, so install from the bundle repo):
   ```bash
   uv tool install git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@main
   ```
3. **`amplifier-gitea` CLI** — install with `uv`:
   ```bash
   uv tool install git+https://github.com/microsoft/amplifier-bundle-gitea@main
   ```
4. **A running Gitea instance with the bundle mirrored.** See the one-time
   setup section below — `amplifier-gitea` provisions one for you.
5. **`ANTHROPIC_API_KEY`** — an Anthropic API key starting with `sk-ant`.
6. **`OPENAI_API_KEY`** — an OpenAI API key starting with `sk-`.

### Verify your environment

Run the following commands and confirm the expected output:

```bash
# 1. Incus is installed and reachable
incus --version
# expected: a version string, e.g. 6.x.x

# 2. amplifier-digital-twin CLI is available
amplifier-digital-twin --version
# expected: a version string

# 3. amplifier-gitea CLI is available
amplifier-gitea --version
# expected: a version string

# 4. Anthropic key is set (first 6 chars should be sk-ant)
echo $ANTHROPIC_API_KEY | head -c 6
# expected: sk-ant

# 5. OpenAI key is set (first 3 chars should be sk-)
echo $OPENAI_API_KEY | head -c 3
# expected: sk-
```

If any check fails, resolve it before proceeding. The DTU passthrough will
forward both keys into the container at launch time; they must be exported in
the host shell.

---

## One-Time Gitea Setup

The DTU profile rewrites GitHub URLs to a local Gitea mirror so that
`amplifier bundle add` installs your local version of the bundle, not the
upstream one on GitHub. The `amplifier-gitea` CLI provisions the Gitea
instance and mirrors the repo for you.

### 1. Create the Gitea environment

```bash
GITEA_JSON=$(amplifier-gitea create --port 10110 --name dtu-memory-gitea)
GITEA_ID=$(echo "$GITEA_JSON" | jq -r .id)
GITEA_URL=$(echo "$GITEA_JSON" | jq -r .gitea_url)
GITEA_TOKEN=$(echo "$GITEA_JSON" | jq -r .token)

echo "ID:    $GITEA_ID"
echo "URL:   $GITEA_URL"
echo "Token: ${GITEA_TOKEN:0:8}..."
```

`amplifier-gitea create` returns a JSON object with the Gitea ID, the host
base URL (`http://localhost:10110`), the admin token, and admin credentials
(`admin` / `admin1234`). Capture all three values for use below.

### 2. Mirror the bundle repo

```bash
amplifier-gitea mirror-from-github "$GITEA_ID" \
  --github-repo https://github.com/michaeljabbour/amplifier-bundle-memory
```

This populates `admin/amplifier-bundle-memory` inside the Gitea instance.
By default only the git history and branches are mirrored — pass
`--include-issues`, `--include-prs`, etc. if you also need metadata.

### Working from a fork

If you are developing on a personal fork rather than the upstream repo,
change the `--github-repo` URL to your fork. The DTU url-rewrite rule
matches `github.com/michaeljabbour/amplifier-bundle-memory`; update the
`url_rewrites.rules[0].match` field in the profile YAML if your fork is at
a different path.

### Generating a fresh token later

If you need a new token without recreating the environment:

```bash
GITEA_TOKEN=$(amplifier-gitea token "$GITEA_ID" | jq -r .token)
```

---

## Launch the DTU

Run the following from the **bundle root** (the directory that contains
this `docs/` folder and `.amplifier/digital-twin-universe/profiles/`):

```bash
DTU_ID=memory-native-e2e

amplifier-digital-twin launch \
  .amplifier/digital-twin-universe/profiles/memory-native-e2e.yaml \
  --name "$DTU_ID" \
  --var GITEA_URL="$GITEA_URL" \
  --var GITEA_TOKEN="$GITEA_TOKEN"
```

The `--name` flag pins the DTU id to a known value so subsequent commands
can reference `${DTU_ID}` directly without parsing JSON. Without it, the id
is auto-generated and printed as the final JSON line of launch output
(e.g. `{"id": "dtu-a1b2c3d4", ...}`).

> **Note on `GITEA_URL`:** `amplifier-digital-twin` automatically rewrites
> `localhost` and `127.0.0.1` in launch variables to the host gateway IP
> reachable from inside the container, so the URL returned by
> `amplifier-gitea create` (e.g. `http://localhost:10110`) works as-is —
> you do not need to substitute a bridge IP yourself.

> **First launch takes 5–10 minutes.** The profile installs system packages,
> a Rust toolchain (amplifier-data's kernel is built from source at install
> time), compiles Python wheels, installs Amplifier, and adds the bundle.
> Subsequent launches reuse the cached base image and are faster.

To validate the migration path from a legacy vendor store instead, launch
`memory-migration-e2e.yaml` the same way.

---

## Two Usage Modes

### Mode 1 — Pytest Integration Tests

Run the full integration suite inside the DTU:

```bash
amplifier-digital-twin exec ${DTU_ID} -- \
  pytest tests/integration/ -v
```

#### Inspecting failures

If a test fails, connect to the container to investigate:

```bash
# Tail the memory event log
amplifier-digital-twin exec ${DTU_ID} -- \
  cat /root/.amplifier/memory/events/*.jsonl | jq .

# Check the memory daemon's discovery file and health
amplifier-digital-twin exec ${DTU_ID} -- \
  bash -c \'cat /root/.amplifier/memory/daemon.json && curl -s $(jq -r .url /root/.amplifier/memory/daemon.json)/health\'

# Run a single failing test with verbose output and log capture
amplifier-digital-twin exec ${DTU_ID} -- \
  pytest tests/integration/test_native_memory_e2e.py -v -s --tb=long
```

### Mode 2 — Interactive Amplifier Session

Open an interactive Amplifier session inside the DTU to manually exercise the
bundle:

```bash
amplifier-digital-twin exec ${DTU_ID} -- amplifier run
```

Once inside, the memory bundle is active. Example queries to try:

- `Search memory for architecture decisions about the daemon lifecycle.`
- `What notes do I have about semantic search configuration?`
- `Store a new memory: the memory daemon auto-starts on first use.`

This mode is useful for exploratory testing, prompt tuning, and verifying that
the briefing hook surfaces the correct project context in the system prompt.

Inspect the live memory store from within an Amplifier session:

```python
# Tail the last 20 events
memory(operation="events", limit=20, tail=True)

# Check memory metadata and drawer counts
memory(operation="status")
```

---

## The Update Loop

When you change bundle code and want to test in the DTU, follow this five-step
cycle:

1. **Edit** — make your changes in
   `amplifier-bundle-memory/behaviors/` or `amplifier-bundle-memory/modules/`.

2. **Commit** — commit the changes locally so they are on a Git ref:
   ```bash
   git -C amplifier-bundle-memory commit -am "wip: <description>"
   ```

3. **Push to Gitea** — push the branch to your Gitea mirror:
   ```bash
   git -C amplifier-bundle-memory push gitea HEAD:main --force
   ```
   If you track a different remote name, substitute it for `gitea`.

4. **Update the DTU** — trigger the in-container update sequence:
   ```bash
   amplifier-digital-twin update ${DTU_ID}
   ```
   This clears the Amplifier module cache and re-runs `amplifier bundle add`,
   fetching the latest commit from Gitea. The memory store is **not** reset
   during an update — accumulated memories survive.

5. **Test** — run the integration suite or an interactive session:
   ```bash
   amplifier-digital-twin exec ${DTU_ID} -- pytest tests/integration/ -v
   ```

Repeat from step 1 as needed. Only steps 2–4 are required for subsequent
iterations if the container is still running.

---

## Troubleshooting

### `uv` bypasses Gitea (URL rewrites not applied)

**Symptom:** `amplifier bundle add` installs from GitHub rather than from your
Gitea mirror. The bundle version inside the DTU does not reflect your local
changes.

**Cause:** The DTU profile sets `allow_uv_github_fast_path: false`. Without
this flag, `uv` uses a native GitHub shortcut that bypasses the mitmproxy HTTPS
proxy. When the fast path is active, URL rewrite rules are never consulted, and
`uv` fetches directly from upstream GitHub.

**Resolution:** The flag is already set correctly in the profile. If you copied
the profile and removed it by accident, add it back:

```yaml
url_rewrites:
  allow_uv_github_fast_path: false
```

Do not remove this flag even if `uv` installation feels slow — without it the
entire point of the mirror is defeated.

---

### `amplifier bundle add` fails with 401 from Gitea

**Symptom:** A provisioning step running `amplifier bundle add ...` exits with
a 401 Unauthorized error.

**Cause:** The Gitea token passed via `--var GITEA_TOKEN=` is expired, revoked,
or was generated for a user that does not have read access to the
`admin/amplifier-bundle-memory` repository.

**Resolution:** Regenerate a fresh token and relaunch:

```bash
NEW_TOKEN=$(amplifier-gitea token <gitea-id> | jq -r .token)
amplifier-digital-twin launch memory-native-e2e \
  --var GITEA_URL="${GITEA_URL}" \
  --var GITEA_TOKEN="${NEW_TOKEN}" \
  | tail -n1
```

---

### Memory daemon never auto-starts / daemon.json missing

**Symptom:** `memory(operation="status")` fails or `/root/.amplifier/memory/daemon.json`
never appears after running a session.

**Cause:** Most commonly a missing Rust toolchain — durable storage requires
the amplifier-data Rust kernel (D10 of the native-cutover design), and
`run_daemon` refuses to start a non-ephemeral store without it.

**Resolution:** Re-launch with `--verbose` and check for a maturin/cargo
build failure during the amplifier-data install step:

```bash
amplifier-digital-twin launch memory-native-e2e \
  --var GITEA_URL="${GITEA_URL}" \
  --var GITEA_TOKEN="${GITEA_TOKEN}" \
  --verbose \
  | tail -n50
```

Confirm `rustc --version` and `cargo --version` succeed inside the container,
and that the amplifier-data git pin's build step completed without error.

---

### Briefing hook does not surface `project-context`

**Symptom:** The system prompt in an interactive session does not include
project-context notes. Tests that assert briefing content fail.

**Cause:** The briefing hook's helper function `_find_project_context_dir`
walks upward from the current working directory looking for a `project-context`
subdirectory. If the CWD inside the session is not under `/workspace`, the walk
will not reach `/workspace/project-context` and the hook returns no content.

**Resolution:**

1. Verify a `project-context/` directory exists under `/workspace`:
   ```bash
   amplifier-digital-twin exec ${DTU_ID} -- ls /workspace/project-context/
   ```
2. Ensure integration tests `cd` to `/workspace` or a subdirectory of it before
   starting an Amplifier session, so `_find_project_context_dir` can locate the
   directory.

---

### API calls fail with "permission denied to anthropic.com"

**Symptom:** LLM calls inside the DTU fail with a network error such as
`ConnectionRefusedError`, `permission denied`, or `ECONNREFUSED` when
connecting to `api.anthropic.com` or `api.openai.com`.

**Cause:** Either:
- The DTU profile's `passthrough.allow_external: true` setting was removed or
  overridden, blocking outbound traffic to external hosts.
- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` were not exported in the host shell
  before running `amplifier-digital-twin launch`, so the keys were not
  forwarded into the container.

**Resolution:**

1. Confirm the profile includes:
   ```yaml
   passthrough:
     allow_external: true
     services:
       - name: anthropic
         key_env: ANTHROPIC_API_KEY
       - name: openai
         key_env: OPENAI_API_KEY
   ```

2. Verify the keys are set in the host shell **before** calling launch:
   ```bash
   echo $ANTHROPIC_API_KEY | head -c 6   # should print sk-ant
   echo $OPENAI_API_KEY | head -c 3      # should print sk-
   ```

3. If the keys were missing at launch time, destroy the environment and
   relaunch after exporting them.

---

### Zero `memory:*` events in events.jsonl

**Symptom:** An Amplifier session runs successfully but
`grep 'memory:' ~/.amplifier/projects/*/sessions/*/events.jsonl`
returns nothing, even though the memory bundle is active.

**Root cause — Amplifier module cache shadowing**

The Amplifier loader prepends `~/.amplifier/cache/amplifier-module-hooks-logging-*/` to
`sys.path` before site-packages. If that cache copy lacks `on_session_ready`, module
detection sets `has_osr=False` and never enqueues the callback. Result:
`register_contributor("observability.events", ...)` is never called, no handlers register
for `memory:*` events, and events.jsonl stays empty.

This also means force-reinstalling the fork package alone is insufficient — the old
cache copy wins over the freshly installed version in site-packages.

**Quick diagnosis:**
```bash
amplifier-digital-twin exec ${DTU_ID} -- \
  /root/.local/share/uv/tools/amplifier/bin/python -c "
import amplifier_module_hooks_logging as m
print(\'file:\', m.__file__)
print(\'has on_session_ready:\', hasattr(m, \'on_session_ready\'))
"
```

If `has on_session_ready: False` — the cache is shadowing the fork.

**Fix:** delete the stale cache directory so Python falls through to
site-packages on the next session start:

```bash
amplifier-digital-twin exec ${DTU_ID} -- bash -c \'
find /root/.amplifier/cache -maxdepth 1 -name "amplifier-module-hooks-logging-*" -exec rm -rf {} + \'
```

No reinstall or primer session required.

---

### "No providers available" after `amplifier-digital-twin update`

**Symptom:** After running `update`, Amplifier sessions fail immediately with
`Error: No providers available`. The provider (Anthropic) is configured in
`~/.amplifier/settings.yaml` but the module isn\'t loading.

**Root cause:** An earlier version of profiles wiped the ENTIRE module cache
directory in the update section, which removed provider-anthropic,
loop-streaming, context-simple, and every other foundation module. Amplifier
cannot start a session until those modules are re-downloaded and cached.

The current profiles do targeted cache invalidation — deleting only the
memory-bundle-specific module cache directories — rather than a full wipe.
Never wipe the entire `/root/.amplifier/cache/` directory in a profile.

**Fix (if you hit this on an old profile version):**
```bash
amplifier-digital-twin exec ${DTU_ID} -- bash -c "
  export PATH=/root/.local/bin:\$PATH
  uv tool install -vv git+https://github.com/microsoft/amplifier
  amplifier --version
"
amplifier-digital-twin exec ${DTU_ID} -- bash -c "
  export PATH=/root/.local/bin:\$PATH
  amplifier bundle add --app \'git+https://github.com/michaeljabbour/amplifier-bundle-memory@main#subdirectory=behaviors/memory.yaml\'
"
```

---

### Debug patches in `amplifier_core/loader.py` break module loading

**Symptom:** All module loads fail with
`UnboundLocalError: cannot access local variable \'sys\' where it is not associated
with a value` at `loader.py, in _load_entry_point, mod = sys.modules.get(module_name)`.

**Root cause:** A debug patch added `import pathlib, sys` inside an `if` block
within `_load_entry_point()`. Python\'s scoping treats any assignment to a name
inside a function (including `import x`) as making that name *local to the entire
function*. When the `if` block is not entered, `sys` is never assigned but Python
still looks for it as a local — causing `UnboundLocalError` every time `sys` is
referenced anywhere else in the function.

**Fix:** locate and remove the offending debug-added import line from the
installed `amplifier_core/loader.py`, then recompile it in place and clear
the stale bytecode cache so the fix takes effect immediately.

**Prevention:** never add `import <name>` inside an `if` block within a function
that also references `<name>` outside the block. Use `import <name> as _<name>`
if a conditional import is genuinely needed, or import at module top level.
