---
bundle:
  name: memory
  version: 0.1.0
  description: Persistent memory system for AI agents - remember observations, track sessions, and surface relevant context

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: memory:behaviors/memory.yaml
---

# Memory System

@memory:context/memory-instructions.md

---

@foundation:context/shared/common-system-base.md
