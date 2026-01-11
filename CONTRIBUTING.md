# Contributing to amplifier-bundle-memory

Thank you for your interest in contributing!

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Create a branch for your changes

## Development Setup

This bundle composes modules from separate repositories:

- [tool-memory](https://github.com/michaeljabbour/amplifier-module-tool-memory)
- [context-memory](https://github.com/michaeljabbour/amplifier-module-context-memory)
- [hooks-memory-capture](https://github.com/michaeljabbour/amplifier-module-hooks-memory-capture)
- [hooks-event-broadcast](https://github.com/michaeljabbour/amplifier-module-hooks-event-broadcast)

For module-level changes, contribute to the appropriate module repository.

## What to Contribute Here

This repository is for:

- Bundle composition and configuration
- Agent definitions (e.g., `memory-curator`)
- Context instructions and documentation
- Integration patterns and examples

## Pull Request Process

1. Ensure your changes follow the [thin bundle pattern](https://github.com/microsoft/amplifier-foundation/blob/main/docs/BUNDLE_GUIDE.md)
2. Update documentation if you change behavior
3. Test your changes with `amplifier run --bundle .`
4. Submit a PR with a clear description of changes

## Code Style

- YAML: 2-space indentation
- Markdown: Follow existing formatting conventions
- Keep bundle.md thin - delegate complexity to behaviors

## Questions?

Open an issue for questions or discussion about potential changes.
