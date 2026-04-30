# Digital Twin Universe (DTU) — End-to-End Test Environment

This guide explains how to provision, use, and maintain the DTU environment for
`amplifier-bundle-memory`. The DTU profile is at:

```
.amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml
```

---

## Why the DTU?

Unit tests for this bundle mock their dependencies to run fast and in isolation:

- `subprocess.run` is patched so the mempalace CLI is never invoked.
- `emit_event` is replaced by an in-process spy.
- MemPalace storage is shadowed by a temp directory or a stub object.

These stubs are valuable for regression safety, but they do not prove the bundle
works in a real Amplifier session. The DTU closes that gap.

Inside a DTU environment the following all run against real infrastructure:

- **Real bundle-install path.** `amplifier bundle add` fetches the bundle from a
  local Gitea mirror using the subdirectory syntax
  (`git+https://...@main#subdirectory=behaviors/mempalace.yaml`). Any packaging
  or manifest error that the mocks hide will surface here.
- **Live MemPalace with semantic search.** The palace is seeded from fixture
  content, and actual OpenAI embedding calls are made during mine and recall
  operations.
- **Real Anthropic / OpenAI calls.** The LLM provider is not stubbed. Prompt
  regressions that do not break unit tests become visible.
- **Full event flow.** Every hook—briefing, post-tool, post-assistant—fires in
  sequence inside a genuine Amplifier session. Ordering bugs and missing awaits
  show up here.

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
DTU_ID=memory-bundle-e2e

amplifier-digital-twin launch \
  .amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml \
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

> **First launch takes 5–10 minutes.** The profile has 15 `setup_cmds` that
> install system packages, compile Python wheels, initialise MemPalace, mine
> fixture content, freeze a reset snapshot, install Amplifier, and add the
> bundle. Subsequent launches reuse the cached base image and are faster.

---

## Three Usage Modes

### Mode 1 — Pytest Integration Tests

Run the full integration suite inside the DTU:

```bash
amplifier-digital-twin exec ${DTU_ID} -- \
  pytest tests/integration/ -v
```

The test suite uses an `autouse` fixture named `reset_palace` that runs
`reset-palace` before each test. This restores the palace to its seeded state
so tests are independent and repeatable.

#### Inspecting failures

If a test fails, connect to the container to investigate:

```bash
# Tail the palace event log
amplifier-digital-twin exec ${DTU_ID} -- \
  cat /root/.mempalace/events/*.jsonl | jq .

# Check palace status
amplifier-digital-twin exec ${DTU_ID} -- \
  mempalace status

# Run a single failing test with verbose output and log capture
amplifier-digital-twin exec ${DTU_ID} -- \
  pytest tests/integration/test_recall.py::test_semantic_search -v -s --tb=long
```

### Mode 2 — Interactive Amplifier Session

Open an interactive Amplifier session inside the DTU to manually exercise the
bundle:

```bash
amplifier-digital-twin exec ${DTU_ID} -- amplifier run
```

Once inside, the memory bundle is active. Example queries to try:

- `Search my palace for architecture decisions about the dual-palace pattern.`
- `What notes do I have about semantic search configuration?`
- `Store a new memory: the DTU reset-palace script restores the seed snapshot.`

This mode is useful for exploratory testing, prompt tuning, and verifying that
the briefing hook surfaces the correct project context in the system prompt.

### Mode 3 — Palace Inspection

Inspect the live palace contents without running tests or a full session.

**From an Amplifier session inside the DTU:**

```python
# Tail the last 20 events
palace(operation="events", limit=20, tail=True)

# Check palace metadata and drawer counts
palace(operation="status")
```

**From a shell inside the DTU:**

```bash
# Inspect all events as JSON
amplifier-digital-twin exec ${DTU_ID} -- \
  bash -c 'cat /root/.mempalace/events/*.jsonl | jq .'

# Reset the palace to its seed state for a clean slate
amplifier-digital-twin exec ${DTU_ID} -- reset-palace
```

Use `reset-palace` whenever you want to start from a known-good state without
re-provisioning the entire environment.

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
   fetching the latest commit from Gitea. The palace is **not** reset during
   an update — accumulated memories survive.

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

**Symptom:** Setup step 14 (`amplifier bundle add ...`) exits with a 401
Unauthorized error during provisioning.

**Cause:** The Gitea token passed via `--var GITEA_TOKEN=` is expired, revoked,
or was generated for a user that does not have read access to the
`admin/amplifier-bundle-memory` repository.

**Resolution:** Regenerate a fresh token and relaunch:

```bash
NEW_TOKEN=$(amplifier-gitea token <gitea-id> | jq -r .token)
amplifier-digital-twin launch memory-bundle-e2e \
  --var GITEA_URL="${GITEA_URL}" \
  --var GITEA_TOKEN="${NEW_TOKEN}" \
  | tail -n1
```

---

### Palace has zero drawers after launch

**Symptom:** `palace(operation="status")` reports 0 drawers, or integration
tests fail because no seed content is found.

**Cause:** Setup step 6 clones the bundle into `/workspace/amplifier-bundle-memory`
and step 7 mines content from
`tests/fixtures/seed-palace/content/`. If the clone path is wrong — for
example because the repo was mirrored under a different name in Gitea — step 7
runs but mines from an empty or non-existent directory.

**Resolution:** Re-launch with `--verbose` to capture the full setup output:

```bash
amplifier-digital-twin launch memory-bundle-e2e \
  --var GITEA_URL="${GITEA_URL}" \
  --var GITEA_TOKEN="${GITEA_TOKEN}" \
  --verbose \
  | tail -n50
```

Confirm that step 6 clones to `/workspace/amplifier-bundle-memory` and step 7
prints a non-zero mine count. If the Gitea repo name is different from
`amplifier-bundle-memory`, update the `clone_addr` destination path in setup
step 6 of the profile YAML.

---

### Briefing hook does not surface `project-context`

**Symptom:** The system prompt in an interactive session does not include
project-context notes. Tests that assert briefing content fail.

**Cause:** The briefing hook's helper function `_find_project_context_dir`
walks upward from the current working directory looking for a `project-context`
subdirectory. If the CWD inside the session is not under `/workspace`, the walk
will not reach `/workspace/project-context` and the hook returns no content.

**Resolution:**

1. Verify the fixture was copied during provisioning:
   ```bash
   amplifier-digital-twin exec ${DTU_ID} -- ls /workspace/project-context/
   ```
   You should see at least one `.md` file.

2. If missing, copy it manually:
   ```bash
   amplifier-digital-twin exec ${DTU_ID} -- \
     cp -r /workspace/amplifier-bundle-memory/tests/fixtures/seed-palace/project-context \
           /workspace/project-context
   ```

3. Ensure integration tests `cd` to `/workspace` or a subdirectory of it before
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
   relaunch after exporting them:
   ```bash
   export ANTHROPIC_API_KEY=<your-key>
   export OPENAI_API_KEY=<your-key>
   amplifier-digital-twin launch memory-bundle-e2e \
     --var GITEA_URL="${GITEA_URL}" \
     --var GITEA_TOKEN="${GITEA_TOKEN}" \
     | tail -n1
   ```
