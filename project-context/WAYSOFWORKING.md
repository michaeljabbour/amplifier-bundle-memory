
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

## Shared dev venv carries an UNPUSHED amplifier-data (2026-09-28)
`~/dev/.venv` now has amplifier-data 0.2.0 installed from the local
`feat/bm25-lens` working tree (`uv pip install --python ~/dev/.venv/bin/python
~/dev/amplifier-data`). Tests there exercise the RRF path; production installs
follow the pin and run legacy until the pin moves. Re-run the install after any
upstream edit; revert with a pin-matching install if another repo needs 0.1.0.

## Run the CI-equivalent root suite at every phase, not just module suites
P2 added `modules/hooks-memory-reflect/tests/conftest.py` with the standard
vendor-absence assert and broke the KG-N4 vendor sweep (its allowlist
enumerates each module conftest). Module suites were green; CI would not have
been. Before each commit batch run:
`~/dev/.venv/bin/python -m pytest tests/test_contract.py tests/test_hook_emissions.py <the list in .github/workflows/contract.yml>`.
A new module with tests must be added to `_ALLOWLISTED_MEMPALACE_FILES` in
`tests/test_vendor_sweep.py`.
