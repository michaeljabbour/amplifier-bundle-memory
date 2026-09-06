# Agent Instructions

## Ecosystem cross-wiring

This repo is the **episodic write target** of the Amplifier behavioral-plasticity constellation. It is one peer in a wired-together set of five repos, governed by a separate conductor bundle (`amplifier-bundle-behavioral-plasticity`); you do not need to read the other repos to work here.

- **Exposes (studs):** `tool-memory`, `hooks-memory-capture`, importance weighting, and the `NativeMemoryStore` seam onto `amplifier-data`.
- **The constellation:** `memory` (this repo) · `context-intelligence-survey` (measurement chassis) · `context-intelligence` (session signal scoring) · `amplifier-data` (append-only substrate) · `behavioral-plasticity` (the conductor that composes all four).
- **Dependency direction is one-way:** `behavioral-plasticity → {survey, CI, memory, amplifier-data}`, and `memory → amplifier-data` (the only intra-four edge). `survey`, `CI`, and `memory` are peers and **MUST NOT** import each other. None of the four may import the `behavioral-plasticity` bundle — it is the only component allowed to know all four.
- **Known primitive gap:** the capture hook (`hooks-memory-capture`) does not yet read `tool_success`; the behavioral-plasticity loop (Step 2) adds it here.

## Project coordination

This project uses a coordination file system in `.amplifier/project-context/`.
These files are this repo's cross-clone memory; they are checked in.

## Starting a Session

The briefing injects the Tier 1 files (`PROJECT_CONTEXT.md`, `GLOSSARY.md`,
`HANDOFF.md`) automatically and lists which coordination files exist. Do not go
looking for one the inventory does not name — it has not been created yet.

## Tier 2 — write these, don't hunt for them

| When you... | Write to |
|-------------|--------|
| Use a term not in the glossary | `GLOSSARY.md` |
| Make a design or architecture decision | `PROVENANCE.md` |
| Hit an error and find the fix | `WAYSOFWORKING.md` |
| Create or move files | `STRUCTURE.md` |
| Run an experiment or benchmark | `EXPERIMENT_JOURNAL.md` |
| Change the project phase or milestone | `PROJECT_CONTEXT.md` |
| Finish any session | `HANDOFF.md` |

## Ending a Session

Update `.amplifier/project-context/HANDOFF.md` with:
- What you accomplished (specific files, decisions, results)
- What's blocked or unresolved
- What the next session should start with
- Non-obvious context the next agent needs
