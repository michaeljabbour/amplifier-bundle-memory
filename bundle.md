---
bundle:
  schema_version: 1
  name: memory
  version: 2.0.0
  description: |
    Local-first AI memory for Amplifier. Combines native semantic vector
    storage (amplifier-data + a local embedder, via an auto-started memory
    daemon), a knowledge graph, structured project coordination files, agent
    diaries, session briefings, and a non-disruptive interject hook that
    fires only when memory is genuinely relevant.

    2.0.0 is a breaking native cutover: the prior vendor-backed store is
    gone. See CHANGELOG.md for migration instructions.

    Credits: Built on the shoulders of open-source memory research.

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: memory:behaviors/memory
---

# Memory System

@memory:context/instructions.md

---

@foundation:context/shared/common-system-base.md
