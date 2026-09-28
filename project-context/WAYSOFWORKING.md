
## Session-tool display redaction can masquerade as file corruption (2026-09-27)
The session's output sanitizer rewrites anything that *looks* like a secret —
including config keys such as `redact_secrets: true` — to `[REDACTED:SECRET]`
in tool output. The file on disk is fine. Verify with a count, not a read:
`grep -cE '^\s*redact_secrets:\s*true\b' behaviors/memory.yaml`.

## amplifier-data pin moves in lockstep across ALL modules
Six module pyprojects restate the pin (tool-memory, capture, briefing,
interject, project-context, behavioral-write). After any bump:
`grep -rl <old-sha> --include=pyproject.toml --include=uv.lock . | grep -v .venv`
must print nothing.
