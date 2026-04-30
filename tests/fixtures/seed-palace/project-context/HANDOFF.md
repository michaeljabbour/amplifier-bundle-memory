# Handoff — 2026-04-29

## Currently working on

Setting up the end-to-end test infrastructure for the memory-bundle DTU profile.
The immediate focus is the seed-palace fixture corpus and the `memory-bundle-e2e.yaml`
DTU profile that consumes it.

## Next steps

1. Write `reset-palace` helper script and add it to the DTU profile `$PATH`.
2. Add `verify-seeding.sh` smoke test asserting ≥ 3 fragments recalled from seed corpus.
3. Extend fixture corpus with a third content file covering hook contract edge-cases.
4. Confirm `allow_uv_github_fast_path: false` propagates to nested sub-installs.
5. Implement deferred full hook tests (currently marked `xfail` pending DTU availability).
6. Verify that behaviour `#subdirectory=` relative source paths resolve correctly from
   inside the DTU container.

## Key decisions made

- **Dual-palace pattern** — seed palace frozen at `~/.mempalace-seed`; reset script
  restores working palace in ~50 ms without re-running `mempalace mine`.
- **`allow_uv_github_fast_path: false`** — required in the DTU profile YAML to prevent
  uv from bypassing the local Gitea mirror when installing bundle dependencies.
- **Real API keys for integration tests** — mock embeddings are insufficient; cost is
  < $0.10 per full run against the seed corpus.

## Open items

- Deferred: full hook tests in `test_hook_emissions.py` are `xfail` pending DTU
  availability in CI.
- Unverified: whether behaviour `#subdirectory=` relative source paths resolve correctly
  when the behaviour file references sibling modules via `../modules/` paths.

---

## Session log

| Date | Summary |
|------|---------|
| 2026-04-29 | Initial DTU profile design; seed-palace fixture corpus created; dual-palace pattern adopted; `allow_uv_github_fast_path: false` fix applied |
