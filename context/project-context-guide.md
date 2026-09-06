# Project Coordination Files

This bundle integrates the **project-context** coordination system: a `.amplifier/project-context/` directory of structured files that persist memory across sessions, across clones, and across AI tools.

**Location.** The files live in `.amplifier/project-context/` — the hidden per-repo directory Amplifier already uses for local state, reachable as `@project:project-context/…`. A top-level `project-context/` is still read when a repo has one, so nothing breaks before migration; `context_dir` in the hook config overrides both. To migrate a repo: `mkdir -p .amplifier && git mv project-context .amplifier/project-context` (add a `!.amplifier/project-context/` negation if your `.gitignore` ignores `.amplifier/`).

## File Tiers

The coordination files are organized by how often they change and how critical they are to read at session start.

**Tier 1 — Read every session** (the Archivist reads these automatically):

| File | Purpose | Update When |
|---|---|---|
| `PROJECT_CONTEXT.md` | Current phase, milestone, team, active work | Phase or milestone changes |
| `GLOSSARY.md` | Canonical terminology with "Means" / "Does NOT Mean" | A new term is used |
| `HANDOFF.md` | Last session summary, blockers, next session start | Every session end |

**Tier 2 — Written on demand, not read speculatively.** These exist only once a session has had something to put in them. The session briefing lists which files are actually present; if one is not listed, create it when you have content for it rather than opening it:

| File | Purpose | Update When |
|---|---|---|
| `STRUCTURE.md` | Directory layout and routing table | Files are created or moved |
| `WAYSOFWORKING.md` | Proven workflows, failure patterns, fixes | A better pattern is found |
| `PROVENANCE.md` | Decision log with context, alternatives, rationale | An architecture decision is made |
| `EXPERIMENT_JOURNAL.md` | Experiments: hypothesis, method, results, learnings | After any experiment or benchmark |

**Tier 3 — Specialized** (ask before generating):

| File | Purpose |
|---|---|
| `CLAIMS_TRACKER.md` | Patent/IP claim tracking with prior art analysis |

## Session Protocol

**At session start**, the `hooks-project-context` hook automatically reads Tier 1 files and injects them into the briefing, prefixed with an inventory of the coordination files that exist. You do not need to read them manually, and you should not open a Tier 2 file the inventory does not name — it is not there.

**During the session**, keep files accurate as you work. This is not extra work — it is part of the work. The rule: if you learned something that would save the next session time, write it down.

**At session end**, the Curator updates `HANDOFF.md` with:
- What was accomplished (specific files, decisions, results)
- What is blocked or unresolved
- What the next session should start with
- Non-obvious context the next agent needs

## Setup

Auto-scaffolding is **disabled by default** (`setup_if_missing: false` in `behaviors/memory.yaml`): the hook reads and updates an existing coordination directory but will not create one in projects that lack it. To scaffold a project deliberately, use the manual command below, or set `setup_if_missing: true` to restore automatic scaffolding everywhere.

```bash
# Manual setup (if needed) — scaffolds .amplifier/project-context/
amplifier run "set up project-context coordination files for this project"
```

## Cross-Tool Compatibility

`AGENTS.md` at the project root is the cross-platform entry point. It is read natively by Amplifier, OpenAI Codex, GitHub Copilot, Cursor, and Windsurf. Claude Code users should symlink: `ln -s AGENTS.md CLAUDE.md`.

The coordination files in `.amplifier/project-context/` complement the native semantic memory index — the files are human-readable and repo-portable; the memory store provides fast semantic retrieval. Both layers work together.
