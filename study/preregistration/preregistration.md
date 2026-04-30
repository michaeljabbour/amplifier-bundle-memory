# Pre-Registration: amplifier-bundle-memory Cross-Session Constraint Adherence Study

**Status:** LOCKED before formal pilot data collection.
**Frozen at:** 2026-04-30 02:00 EDT (commit-pinned harness, prompts, scorer)
**Author:** automated overnight pilot driven from a single user-approved prompt
("C — I'd love to wake up with a completed experiment and academic paper")

This document is the prospective analysis plan for a pilot study on whether the
`amplifier-bundle-memory` Amplifier bundle reduces the need for a user to
re-state previously established conventions in a fresh session. It is committed
to disk before any formal trial is collected; only smoke-test trials (used to
validate the harness) preceded this document.

## 1. Background and motivation

A user proposed the hypothesis:

> "The amplifier-bundle-memory bundle helps users stop repeating themselves
> in-session and across sessions."

In-session repetition is mostly a property of the LLM context window and is
not the bundle's distinctive contribution. We focus on **cross-session**
repetition: the user states a convention once in Session 1, does not restate
it in Session 2 (which is a new conversation), and we measure whether the
assistant's S2 output respects the convention.

`amplifier-bundle-memory` ships three relevant mechanisms:
1. **Briefing hook** — fires on `session:start`, attempts to surface palace
   contents and `project-context/*.md` files into the system prompt.
2. **Capture hook** — fires on `tool:post`, queues tool-result snippets into
   the palace via the spool/drain pipeline.
3. **Palace tool** — exposed to the LLM; the model can call
   `palace.search(...)` to retrieve memories on demand.

Smoke testing revealed that under our default `--mode single` invocation
(tool-use disabled) the practically active channel is the briefing hook
auto-loading `project-context/*.md`. The capture hook does not fire (no tool
events) and the palace tool is not invoked (no exploration). This study
therefore tests **the briefing hook's project-context auto-load mechanism**
specifically, which is a faithful slice of the bundle's behavior in the
common "I have a stable conventions file" workflow.

## 2. Pre-registered hypothesis (H1)

> **H1 (primary, causal):** Composing `amplifier-bundle-memory` into an
> otherwise-identical Amplifier session, with a non-empty
> `/workspace/project-context/CONVENTIONS.md` documenting three project
> conventions, causes a higher rate of all-three-constraint adherence in a
> Session 2 whose user message does not restate those conventions, compared
> to a control arm where the bundle is not composed and `project-context`
> does not exist.

## 3. Sub-claims (locked)

- **C1 (primary effect)** — the with-memory arm achieves a higher all-three
  pass rate on UC-1 than the without-memory arm.
- **C2 (no-regression guardrail)** — the with-memory arm does not reduce
  the rate of *syntactic validity* of generated Python code blocks by more
  than 5 percentage points relative to the control. (We deliberately downgrade
  the "functional correctness" guardrail discussed in critic findings to
  "syntactic validity," since `--mode single` produces snippets, not runnable
  full apps. This is documented as a known limitation in §10.)
- **C3 (descriptive)** — per-constraint pass rates (no_pip, no_comments,
  kebab_case_routes) trend in the same direction as the composite. Reported
  as sensitivity, not confirmatory.

## 4. Design

| Element | Specification |
|---|---|
| Use case | UC-1 Constraint Adherence (Flask API conventions) |
| Constraints (3) | (a) no `pip install` / `requirements.txt` instructions; (b) no Python comments or docstrings in extracted code blocks; (c) all detected Flask routes are kebab-case (lowercase, digits, hyphens, slashes only). |
| Arms | `with-memory` (DTU `memory-bundle-e2e`, `amplifier-bundle-memory` composed, `/workspace/project-context/CONVENTIONS.md` present) ; `without-memory` (DTU `study-without-memory`, no memory bundle, no `project-context/`). Both DTUs share Ubuntu 24.04 base, same Amplifier install via the same Gitea mirror, same `provider-anthropic` (`claude-sonnet-4-6` as default model, default temperature). |
| Provider model | anthropic / claude-sonnet-4-6 (default temp; default max_tokens). Pinned in DTU `settings.yaml`. |
| Run mode | `amplifier run --mode single --output-format json` (no tool use, single LLM call). |
| Trial | One paired trial = one S1 priming session + one S2 target session per arm. S1 is run from a fresh `/workspace/work_<trial_id>/` workspace; that workspace is deleted before S2 (palace persists in with-memory; nothing persists in without-memory). |
| Interleaving | Within each pair (k = 1..N), arms are executed in a randomized order using a fixed seed. This distributes any provider-side drift across arms. |
| Palace reset | `reset-palace` is invoked before every S1 in the with-memory arm so the palace state is identical at the start of each trial. |
| Project-context contents | `CONVENTIONS.md` in the with-memory arm states the same three conventions that the S1 user message asks the model to follow. The same conventions appear in S1 user messages in *both* arms (so S1 priming is identical); the difference between arms is what S2 inherits. |
| S1 prompt | Frozen in `study/harness/prompts.json::uc1_constraint_adherence.S1`. |
| S2 prompt | Frozen in `study/harness/prompts.json::uc1_constraint_adherence.S2`. Does NOT restate constraints. |
| Scorer | Frozen in `study/harness/scorer.py`. Constraints are auto-graded via regex + ast + tokenize. |
| Sample size | N = 20 paired trials (40 sessions per arm = 80 LLM calls total). |
| Extension rule | If after N=20, either arm is at 0/20 or 20/20 (floor or ceiling), automatically extend to N=40 and report both the original-N and extended-N results. |

## 5. Primary statistical test

- **Test:** McNemar's exact test (binomial on discordant pairs) on the binary
  all-three outcome, two-sided, α = 0.05.
- **Effect size:** odds ratio (with exact 95% CI via the discordant-pair
  binomial), AND risk difference `p_with − p_without` with Wilson 95% CIs on
  each arm independently.
- **Minimum effect of practical interest:** risk difference ≥ +20 percentage
  points (with-memory − without-memory). A statistically significant smaller
  lift will be reported as "detected but below MEPI" rather than as
  confirming the user's plain-English claim.

The MEPI was lowered from the +30pp originally proposed (in the
hypothesis-designer output) to +20pp after smoke-test inspection suggested
that the without-memory baseline may not be at floor — the model has
mild intrinsic preference for Flask idioms that happen to satisfy parts of
the kebab-case constraint without instruction. The +20pp threshold is set
*before* any pilot trial is collected.

## 6. Secondary outcomes (descriptive, no inferential claim)

- **Per-constraint pass rates** — `no_pip`, `no_comments`,
  `kebab_case_routes` reported separately. Wilson 95% CIs.
- **Per-constraint paired discordance** — for each constraint, the paired
  (without_pass, with_pass) cell counts. Reported as raw 2x2 tables.
- **Mean S2 response length** — proxy for verbosity differences between
  arms.

## 7. Disconfirmation criteria (what falsifies H1)

H1 is considered **falsified** if any of:

1. McNemar p ≥ 0.05 on the all-three outcome at N=20 (and N=40 if extended).
2. Risk difference < 0 (control arm beats treatment) on the all-three
   outcome.
3. Risk difference ∈ (0, +20pp) — detected but below MEPI; reported as
   "directional evidence but not supporting the practical claim."
4. With-memory syntactic-validity rate is more than 5pp BELOW
   without-memory at N=20 (guardrail breach).

Any other outcome (RD ≥ +20pp AND p < 0.05 AND no guardrail breach) is
reported as "consistent with H1 in this pilot, with the limitations of §10."

## 8. Resumability and data integrity

- Trial outputs are written as JSONL streamed to disk with each trial's full
  amplifier response captured raw before scoring.
- Re-running the harness with the same `--label` skips trial IDs already in
  `results_<label>.jsonl`.
- All trial results, raw S1/S2 responses, and scorer outputs are committed
  to the repository as supplementary materials.

## 9. Author degrees of freedom — pre-registered restrictions

- The scorer (`scorer.py`) was committed before any formal trial. Any
  modification after the first formal trial invalidates the run; the
  modification will be documented and the run will be discarded.
- The prompts (`prompts.json`) were last modified after smoke-test inspection
  showed a ceiling effect on the original simpler S2 prompt. They are now
  frozen. Any modification after the first formal trial invalidates the run.
- The DTU profiles, model, and `--mode single` invocation are pre-registered.

## 10. Known limitations (pre-registered)

These are limitations we accept up front; they bound the conclusion the paper
can draw:

1. **One scenario, one model, one bundle, single-shot mode.** UC-1 only;
   `claude-sonnet-4-6` only; `amplifier-bundle-memory` only; `--mode single`
   only. Generalization to other tasks, models, bundles, or interactive
   tool-using sessions is not warranted by this pilot.
2. **Briefing hook is the only differentiating mechanism in scope.** The
   capture hook and the palace tool are inactive under `--mode single`. We
   cannot attribute any observed effect to the palace's semantic recall or
   to the bundle's mining pipeline.
3. **Project-context contents.** `CONVENTIONS.md` was authored by the study
   author; its phrasing may either help or hinder constraint transmission
   relative to natural user-authored conventions.
4. **Static, not dynamic, code analysis.** Functional correctness of
   generated code is replaced with syntactic validity (ast.parse). A response
   that imports a missing package or uses an undefined name passes the
   guardrail.
5. **Confound: filesystem visibility of `project-context/`.** Under
   `--mode single` no tools are available, so the model cannot read the file
   directly. Under tool-use mode it could. We pre-registered `--mode single`
   precisely to remove this confound.
6. **Sample size is small.** N=20 paired is a pilot, not a definitive study.
7. **Same-machine, same-day execution.** Provider-side drift over multi-day
   data collection is not assessed.
8. **Single rater (the deterministic scorer).** No human inter-rater
   reliability check.

## 11. Reporting

The paper that follows the pilot run will report:

- The primary McNemar p-value and effect size with CI.
- All secondary outcomes.
- All known violations or anomalies encountered during execution
  (timeouts, parse failures, retries) without selective omission.
- Whether the +20pp MEPI was met.
- Whether the guardrail was breached.

If the run is null, the paper still reports it. If the run is positive, the
paper carefully scopes the claim to what was tested, per §10.
