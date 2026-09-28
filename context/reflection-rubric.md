# Reflection rubric

How to turn one conversation span into durable facts. Written for the
`memory:distiller` agent; the rules are ours, derived from what Letta Code,
Mem0, Hindsight, memU and Graphiti each learned (see
`docs/research/2026-09-27-memory-systems-gene-survey.md`).

## The one rule
The verbatim span is already stored and searchable. **Only write what a future
session would get wrong without being told.** Writing nothing is a correct,
common outcome.

## What to look for, in priority order
1. **Corrections** — the user or a test told the agent it was wrong. ("Don't
   use pip here, the repo uses uv.")
2. **Preferences and standing instructions** — how this person or project
   wants work done.
3. **Durable facts about the project or world** — where things live, what a
   component does, which version is pinned, what a decision was and why.
4. **Contradictions** — something now true that makes an existing fact false.
5. **Procedures** — a sequence that worked and will be needed again.

## What to drop
- Anything ephemeral: this turn's progress, transient errors that were fixed,
  intermediate reasoning, tool chatter.
- Anything already captured: search memory first; if a current fact already
  says it, add evidence to it instead of writing a new one.
- Anything not generalizable beyond this span.
- Anything secret-shaped. If you see a key or password, write nothing about it.

## How to write a fact
- One idea per fact, **self-contained**, 15–80 words. A stranger must
  understand it without the span.
- Name things fully: repo, file path, function, package, person.
- **Absolute dates only.** Resolve "yesterday", "last week", "recently"
  against the span's observation date (given in the job), never today's date.
- Say who asserted it when it matters ("Michael prefers…", "the test suite
  showed…").
- No computation or inference beyond what the span states.
- Pick `fact_type`: `correction`, `preference`, `world`, `experience`,
  `procedure`.
- Cite evidence: every fact's `source_refs` includes the job's span drawer,
  plus any other drawer you relied on.
- If the fact is a single-valued setting (package manager, test runner,
  default branch, pinned version of X), set `predicate` so newer values
  supersede older ones automatically.

## Updating what exists
- Newer information that replaces an older fact → `supersedes` the old ref.
  History is kept; nothing is deleted.
- Two facts that conflict and you cannot tell which is right → write the new
  one with `conflicts_with` the old; a person resolves it later.
- Prefer strengthening an existing fact (same text, new evidence) over creating
  a near-duplicate.

## Finish
Call `reflection_job_done` with the refs you wrote, or `noop: true` and a
one-line reason. Never leave a job open.
