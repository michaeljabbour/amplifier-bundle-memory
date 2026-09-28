
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

## Shared dev venv tracks the amplifier-data pin (2026-09-28)
`~/dev/.venv` has amplifier-data installed from the pinned, pushed commit
(`2ecdce3`, includes the BM25 lens). After any pin bump, reinstall from the pin:
`uv pip install --python ~/dev/.venv/bin/python "amplifier-data @ git+https://github.com/michaeljabbour/amplifier-data@<sha>" --reinstall-package amplifier-data`
and confirm `import amplifier_data.lenses.bm25` works.

## Run the CI-equivalent root suite at every phase, not just module suites
Module suites can be green while the root gate suite fails (it scans the
whole repo, not one module). Before each commit batch run:
`~/dev/.venv/bin/python -m pytest tests/test_contract.py tests/test_hook_emissions.py <the list in .github/workflows/contract.yml>`.
That list includes `tests/test_no_product_names.py`, the gate that fails the
build if a prior-generation vendor/product name reappears anywhere outside
its allowlist (see the module docstring for the exact allowlist).
