# Glossary — memory-bundle

| Term | Definition |
|------|-----------|
| **Palace** | The MemPalace persistent store.  Holds all captured memory fragments indexed by embedding vector.  Lives at `~/.mempalace/` by default. |
| **Drawer** | A named collection inside a Palace.  Fragments are organised into drawers by category (e.g. `decisions`, `learnings`, `patterns`).  Each drawer has its own embedding index. |
| **Wing** | A top-level namespace inside a Palace, grouping multiple drawers.  Typically corresponds to a project or domain.  Wings allow multiple projects to share a single Palace without cross-contamination. |
| **Room** | A sub-division of a Drawer used for fine-grained access control and retrieval scoping.  Rooms are optional; a Drawer with no rooms behaves as a single flat collection. |
| **Briefing** | The session-start context injection produced by the briefing hook.  Combines semantic recall results from the Palace with verbatim project-context documents to give the agent a situational summary at the start of a session. |
| **Spool** | A directory (`/workspace/spool/` in the DTU) used as a durable intermediate queue by the capture hook.  Raw fragments are written to the spool synchronously on the hot path; the drain thread reads from the spool and performs the slow embedding + palace-write asynchronously. |
| **Seed palace** | A pre-seeded Palace snapshot used in the DTU profile for end-to-end testing.  Built once at container-init time by running `mempalace mine` against the fixture corpus and frozen to `~/.mempalace-seed`.  Restored by the `reset-palace` script between test runs. |
| **Drain thread** | A background thread (or subprocess for large payloads) that reads fragments from the spool directory, calls the embedding API, and writes the resulting vectors to the Palace.  Decouples API latency from tool response latency. |
| **Capture hook** | The Amplifier hook that fires on `tool:post` events and enqueues the tool result as a memory fragment.  Emits `capture_queued` or `capture_skipped` synchronously, then hands off to the drain thread. |
| **Briefing hook** | The Amplifier hook that fires on `session:start` and injects a briefing (Palace recall + project-context documents) into the session context. |
