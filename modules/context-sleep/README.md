# amplifier-module-context-sleep

Sleep-time context consolidation for Amplifier sessions.

Implements the full `ContextManager` protocol with a two-buffer architecture that compresses verbose, redundant context using an LLM (or a verbatim fallback when no provider is available) while preserving the full raw history.

---

## What it does

When the live working window exceeds a configurable token threshold, the module:

1. **Evicts** the oldest messages from the working window (keeping the last `keep_recent_messages` verbatim).
2. **Consolidates** the evicted text into a single compact `[Consolidated memory]` system message using the **faithful** LLM prompt (see below).
3. **Rebuilds** `_working` as `[consolidated-memory msg] + [recent verbatim tail]`.
4. **Leaves `_raw` untouched** — `get_messages()` always returns the complete unmodified history.

---

## Two-buffer contract

| Buffer | Modified by compact? | Returns |
|--------|---------------------|---------|
| `_raw` | **Never** | `get_messages()` — full verbatim history |
| `_working` | Yes | `get_messages_for_request()` — compact live window |

The non-destructive invariant on `_raw` is a hard contract requirement.

---

## Installation

```toml
# In a bundle's behaviors/ YAML:
context:
  module: context-sleep
  source: git+https://github.com/michaeljabbour/amplifier-bundle-memory@main#subdirectory=modules/context-sleep
```

Or as a local path during development:

```toml
context:
  module: context-sleep
  source: ./modules/context-sleep
```

---

## Configuration

All keys are optional.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `consolidation_threshold_tokens` | int | `8000` | Token count of `_working` that triggers compaction |
| `keep_recent_messages` | int | `20` | Number of recent messages kept verbatim after eviction |
| `consolidation_provider` | str | `None` | Provider name to use for consolidation (picks first if unset) |
| `style` | str | `"faithful"` | Consolidation style: `"faithful"` or `"creative"` |
| `enabled` | bool | `True` | Set to `false` to disable auto-compaction |

Example bundle config:

```yaml
session:
  context: context-sleep
  context_config:
    consolidation_threshold_tokens: 12000
    keep_recent_messages: 30
    style: faithful
```

---

## Empirical guardrails

These guardrails are derived from a controlled study of LLM-based context consolidation on a multi-hop QA benchmark.  **Do not change the default settings without re-running the study.**

### Faithful consolidation ≈ verbatim retention (for atomic facts)

Faithful LLM consolidation is statistically equivalent to verbatim retention on atomic-fact recall tasks.  The value of this module is **compressing redundant or verbose context** (long tool outputs, repeated boilerplate, iterative debugging noise) — not improving the model's reasoning over already-atomic facts.

> Rule: use this module when your sessions accumulate large volumes of repetitive tool output.  Do not expect reasoning gains over dense, non-redundant context.

### Creative / reorganise mode HURTS (~−28 pp)

The `"creative"` style instructs the LLM to reorganise and derive higher-level insights from the context.  In the study, this mode fabricated approximately **50–75 % of facts** compared to the source, reducing downstream task accuracy by ~28 percentage points.

The module always emits a prominent `WARNING` log when `style="creative"` is set.  **This mode is for research comparison only.**  Default is `"faithful"`.

### Representation format matters (~+19 pp)

Consolidations expressed as **natural-language prose** outperform symbolic or triple-notation formats by approximately 19 percentage points on fact-recall tasks.  The faithful prompt explicitly instructs the model to produce natural-language output and prohibits symbol/triple notation.

### Single pass is optimal (H3 null result)

The hypothesis that multiple consolidation passes improve downstream accuracy was not supported.  The module performs exactly **one pass** per compaction trigger.  There is no multi-pass option.

---

## Events emitted

| Event | Payload fields | When |
|-------|---------------|------|
| `context:pre_compact` | `message_count`, `token_count` | Before eviction |
| `context:post_compact` | `message_count`, `token_count` | After eviction |
| `context:sleep_complete` | `note`, `evicted_count` | After successful consolidation |

---

## Fallback behaviour (no provider)

When no LLM provider is available (or the provider call fails), the module uses **verbatim deduplication**:

- All evicted message text is concatenated.
- Purely duplicated lines (exact string match after strip) are removed.
- All unique lines are preserved — no data is ever silently discarded.

This ensures the module is safe to use even in sessions without a provider configured.

---

## Protocol methods

All methods are `async`.

| Method | Description |
|--------|-------------|
| `add_message(msg)` | Append to `_raw` and `_working`; compact if threshold exceeded |
| `get_messages_for_request(token_budget, provider)` | Return `_working` (cheap) |
| `get_messages()` | Return full `_raw` copy (non-destructive) |
| `set_messages(messages)` | Set both buffers (session resume) |
| `clear()` | Clear both buffers |
| `should_compact()` | True when `_working` exceeds the token threshold |
| `compact()` | Trigger one consolidation pass explicitly |

---

## Architecture

```
add_message(m)
    │
    ├─► _raw.append(m)          ← append-only, never compacted
    │
    ├─► _working.append(m)
    │
    └─► should_compact()?
            │ Yes
            ▼
        compact()
            │
            ├─ evicted = _working[:-keep_recent]  (excl. prior memory msg)
            ├─ keep    = _working[-keep_recent:]
            │
            ├─ provider? ──► _call_provider(evicted_text, style)
            │                    ├─ success → note = LLM output
            │                    └─ failure → note = _verbatim_fallback(evicted_text)
            │
            └─ _working = [{"role":"system","content":"[Consolidated memory]\n<note>"}]
                        + keep
```

---

## Running tests

```bash
cd /path/to/amplifier-bundle-memory
python -m pytest tests/test_context_sleep.py -v
```

Requirements: `amplifier-core` must be installed (tests skip gracefully if absent).
