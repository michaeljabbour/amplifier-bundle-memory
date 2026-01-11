# amplifier-bundle-memory

Persistent memory system for Amplifier - enables AI agents to remember observations, track sessions, and surface relevant context across conversations.

## Features

- **Persistent Storage**: SQLite database with FTS5 full-text search
- **Automatic Capture**: Hook-based observation capture from tool outputs
- **Progressive Disclosure**: Token-budgeted context injection at session start
- **Session Tracking**: Summaries with request/investigated/learned/completed
- **Event Broadcasting**: Transport-agnostic real-time updates for UIs
- **Memory Curation**: Specialized agent for organizing and maintaining memories

## Architecture

This bundle composes four modules into a cohesive memory system:

```
┌─────────────────────────────────────────────────────────────┐
│                     Memory Bundle                            │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌──────────────────┐                    │
│  │ context-    │    │ hooks-memory-    │                    │
│  │ memory      │    │ capture          │                    │
│  │             │    │                  │                    │
│  │ Injects     │    │ Observes tools   │                    │
│  │ relevant    │    │ Captures         │                    │
│  │ context     │    │ learnings        │                    │
│  └──────┬──────┘    └────────┬─────────┘                    │
│         │                    │                              │
│         ▼                    ▼                              │
│  ┌─────────────────────────────────────┐                    │
│  │           tool-memory               │                    │
│  │                                     │                    │
│  │  SQLite + FTS5 persistent store     │                    │
│  │  Observation types & concepts       │                    │
│  │  Session tracking                   │                    │
│  └─────────────────────────────────────┘                    │
│                      │                                      │
│                      ▼                                      │
│  ┌─────────────────────────────────────┐                    │
│  │       hooks-event-broadcast         │                    │
│  │                                     │                    │
│  │  WebSocket / SSE / Console          │                    │
│  │  Real-time UI updates               │                    │
│  └─────────────────────────────────────┘                    │
├─────────────────────────────────────────────────────────────┤
│  Agent: memory-curator                                      │
│  Reviews, organizes, and maintains the memory store         │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### Direct Invocation

```bash
amplifier run --bundle git+https://github.com/michaeljabbour/amplifier-bundle-memory@main \
  "What gotchas have I discovered in this codebase?"
```

### Registry-Based

```bash
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-bundle-memory@main
amplifier bundle use memory
amplifier run "Review my recent sessions"
```

### Include in Another Bundle

```yaml
includes:
  - bundle: git+https://github.com/michaeljabbour/amplifier-bundle-memory@main
```

### Behavior-Only (without full bundle)

```yaml
includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: git+https://github.com/michaeljabbour/amplifier-bundle-memory@main#path=behaviors/memory.yaml
```

## Modules

| Module | Type | Description |
|--------|------|-------------|
| [tool-memory](https://github.com/michaeljabbour/amplifier-module-tool-memory) | Tool | Persistent SQLite storage with FTS5 search |
| [context-memory](https://github.com/michaeljabbour/amplifier-module-context-memory) | Context | Progressive disclosure context injection |
| [hooks-memory-capture](https://github.com/michaeljabbour/amplifier-module-hooks-memory-capture) | Hook | Automatic observation capture from tools |
| [hooks-event-broadcast](https://github.com/michaeljabbour/amplifier-module-hooks-event-broadcast) | Hook | Transport-agnostic event relay |

## Agent

### memory-curator

Curates, organizes, and maintains the memory store. Activated when users ask about:
- Reviewing or cleaning up memories
- Finding patterns across observations
- Understanding what has been learned
- Consolidating duplicate memories

## Observation Types

- `bugfix` - Something broken, now fixed
- `feature` - New capability added
- `refactor` - Code restructured, behavior unchanged
- `change` - Generic modification
- `discovery` - Learning about existing system
- `decision` - Architectural choice with rationale

## Concept Types

- `how-it-works` - Mechanism or process
- `why-it-exists` - Design rationale
- `problem-solution` - Problem and resolution
- `gotcha` - Non-obvious pitfall
- `pattern` - Recurring structure
- `trade-off` - Competing concerns

## Configuration

Override defaults in your bundle:

```yaml
includes:
  - bundle: git+https://github.com/michaeljabbour/amplifier-bundle-memory@main

# Override tool-memory config
tools:
  - module: tool-memory
    config:
      storage_path: ~/custom/path/memories.db
      max_memories: 5000

# Override context-memory config
session:
  memory_context:
    config:
      token_budget: 4000
      lookback_days: 180
```

## Requirements

- Python 3.11+
- amplifier-core
- amplifier-foundation

## License

MIT License - See LICENSE file
