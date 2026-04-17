# Project Coordination Files

This bundle integrates the **project-context** coordination system. Every project using this bundle gets a `project-context/` directory containing structured files that persist memory across sessions, across clones, and across AI tools.

## File Tiers

The coordination files are organized by how often they change and how critical they are to read at session start.

**Tier 1 — Read every session** (the Archivist reads these automatically):

| File | Purpose | Update When |
|---|---|---|
| `project-context/PROJECT_CONTEXT.md` | Current phase, milestone, team, active work | Phase or milestone changes |
| `project-context/GLOSSARY.md` | Canonical terminology with "Means" / "Does NOT Mean" | A new term is used |
| `project-context/HANDOFF.md` | Last session summary, blockers, next session start | Every session end |

**Tier 2 — Read when relevant**:

| File | Purpose | Update When |
|---|---|---|
| `project-context/STRUCTURE.md` | Directory layout and routing table | Files are created or moved |
| `project-context/WAYSOFWORKING.md` | Proven workflows, failure patterns, fixes | A better pattern is found |
| `project-context/PROVENANCE.md` | Decision log with context, alternatives, rationale | An architecture decision is made |
| `project-context/EXPERIMENT_JOURNAL.md` | Experiments: hypothesis, method, results, learnings | After any experiment or benchmark |

**Tier 3 — Specialized** (ask before generating):

| File | Purpose |
|---|---|
| `project-context/CLAIMS_TRACKER.md` | Patent/IP claim tracking with prior art analysis |

## Session Protocol

**At session start**, the `hooks-project-context` hook automatically reads Tier 1 files and injects them into the briefing. You do not need to read them manually.

**During the session**, keep files accurate as you work. This is not extra work — it is part of the work. The rule: if you learned something that would save the next session time, write it down.

**At session end**, the Curator updates `HANDOFF.md` with:
- What was accomplished (specific files, decisions, results)
- What is blocked or unresolved
- What the next session should start with
- Non-obvious context the next agent needs

## Setup

If `project-context/` does not exist in the current project, the `hooks-project-context` hook scaffolds it automatically using the templates from the project-context system. The generated files are customized to the project by scanning the codebase structure.

```bash
# Manual setup (if needed)
amplifier run "set up project-context coordination files for this project"
```

## Cross-Tool Compatibility

`AGENTS.md` at the project root is the cross-platform entry point. It is read natively by Amplifier, OpenAI Codex, GitHub Copilot, Cursor, and Windsurf. Claude Code users should symlink: `ln -s AGENTS.md CLAUDE.md`.

The coordination files in `project-context/` complement the MemPalace semantic index — the files are human-readable and repo-portable; the palace provides fast semantic retrieval. Both layers work together.
