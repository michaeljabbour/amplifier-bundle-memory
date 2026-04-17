---
bundle:
  schema_version: 1
  name: memory
  version: 1.2.0
  description: |
    Local-first AI memory for Amplifier. Combines semantic vector storage,
    a knowledge graph, structured project coordination files, and a SQLite
    fact store into a single cohesive five-tier memory layer. Provides
    verbatim retrieval (96.6% R@5 on LongMemEval), agent diaries, session
    briefings, and a non-disruptive interject hook that fires only when
    memory is genuinely relevant.

    Credits: Built on the shoulders of open-source memory research.

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: memory:behaviors/mempalace
---

# Memory System

@memory:context/instructions.md

---

@foundation:context/shared/common-system-base.md
