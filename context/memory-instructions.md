# Memory System Instructions

You have access to a persistent memory system that helps you remember observations, track sessions, and surface relevant context across conversations.

## How Memory Works

### Automatic Capture
The `hooks-memory-capture` module automatically observes your tool usage and captures significant learnings. You don't need to explicitly save most things - important discoveries, bugfixes, and decisions are recorded automatically.

### Context Injection
At the start of each session, `context-memory` injects relevant memories based on:
- Your current working directory/project
- Recent session summaries
- Relevance to the current prompt

This appears as an **index view** with titles and token estimates. Use `get_memory(id)` to retrieve full details when needed.

### Manual Memory Tools
When you want to explicitly remember something important:

| Tool | When to Use |
|------|-------------|
| `add_memory` | Store a specific observation with classification |
| `search_memories` | Find relevant past knowledge |
| `get_memory` | Retrieve full details of an indexed memory |
| `update_memory` | Correct or enhance existing memories |
| `get_file_context` | Get memories related to a specific file |

## Observation Types

Classify memories appropriately:
- **bugfix** - Something was broken, now fixed
- **feature** - New capability added
- **refactor** - Code restructured, behavior unchanged
- **change** - Generic modification
- **discovery** - Learning about existing system
- **decision** - Architectural choice with rationale

## Concept Types

Add concept classification for knowledge organization:
- **how-it-works** - Mechanism or process explanation
- **why-it-exists** - Rationale for design choice
- **problem-solution** - Problem and its resolution
- **gotcha** - Non-obvious behavior or pitfall
- **pattern** - Recurring approach or structure
- **trade-off** - Competing concerns and balance

## Best Practices

1. **Let automatic capture work** - Most valuable memories are captured from tool outputs
2. **Add explicit memories for decisions** - Rationale for choices is rarely captured automatically
3. **Use search before asking** - Check if you've encountered something before
4. **Update importance** - Boost frequently-referenced memories
5. **Track gotchas explicitly** - Non-obvious pitfalls deserve manual `add_memory`

## Available Agent

- **memory-curator**: For reviewing, organizing, and maintaining memories. Delegate to this agent when users ask about what has been learned, want to clean up memories, or need pattern analysis across observations.

## Session Tracking

Sessions are tracked automatically with:
- `request` - What was asked
- `investigated` - What was explored
- `learned` - Key takeaways
- `completed` - What was accomplished
- `next_steps` - Suggested follow-ups

Access via `list_sessions` and `get_session` tools.
