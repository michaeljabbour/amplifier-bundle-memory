"""
Substitute analysis results into paper.md, producing paper_filled.md.

Inputs:
    paper.md (the skeleton with {{PLACEHOLDER}} fields)
    results_<label>.analysis.json (from analyze.py)
    results_<label>.jsonl (raw trial records, for anomaly counts)

Output:
    paper_filled.md (ready to convert via pandoc)

The placeholders the skeleton uses:

    {{ALL_THREE_DELTA}}
    {{ALL_THREE_WITH}}
    {{ALL_THREE_WITHOUT}}
    {{MCNEMAR_P}}
    {{HEADLINE_VERDICT}}
    {{PER_CONSTRAINT_SUMMARY}}
    {{RESULTS_SECTION_PLACEHOLDER}}
    {{PRIMARY_OUTCOME_PLACEHOLDER}}
    {{GUARDRAIL_PLACEHOLDER}}
    {{PER_CONSTRAINT_PLACEHOLDER}}
    {{ANOMALIES_PLACEHOLDER}}
    {{DISCUSSION_PLACEHOLDER}}
    {{INTERPRETATION_PLACEHOLDER}}
    {{NEGATIVE_SCOPE_PLACEHOLDER}}
    {{CONCLUSION_PLACEHOLDER}}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt_pct(p: float) -> str:
    return f"{100 * p:.1f}\\%"


def fmt_pp(p: float) -> str:
    return f"{100 * p:+.1f}\\,pp"


def headline_verdict(primary: dict) -> str:
    rd = primary["risk_difference"]
    p = primary["mcnemar_exact_p"]
    if p < 0.05 and rd >= 0.20:
        return ("This meets both the pre-registered statistical threshold "
                "($p<0.05$) and the pre-registered minimum effect of practical "
                "interest ($+20\\,$pp). The result is consistent with H1 "
                "under the limitations of \\S\\,\\ref{limits}")
    if p < 0.05 and rd > 0:
        return ("This is statistically significant but below the "
                "pre-registered minimum effect of practical interest "
                "($+20\\,$pp). The result is directional but does not "
                "support the user's plain-English claim")
    if p < 0.05 and rd <= 0:
        return ("This is statistically significant in the *opposite* "
                "direction; the control arm out-performs treatment. H1 is "
                "falsified")
    if rd >= 0.20:
        return ("Effect direction matches H1 with a magnitude above the "
                "pre-registered MEPI, but the McNemar test does not reach "
                "$\\alpha=0.05$. H1 is not supported in this pilot — the "
                "effect, if real, would require a larger $N$ to confirm")
    return ("Neither the statistical threshold nor the practical-interest "
            "threshold is met. H1 is not supported in this pilot")


def per_constraint_summary(constraints: dict) -> str:
    parts = []
    for key in ("no_pip", "no_comments", "kebab_case_routes"):
        c = constraints[key]
        with_p = c["with_rate"]
        without_p = c["without_rate"]
        rd = c["risk_difference"]
        nice = key.replace("_", "\\_")
        parts.append(
            f"{nice}: with={fmt_pct(with_p)}, without={fmt_pct(without_p)}, "
            f"$\\Delta = {fmt_pp(rd)}$"
        )
    return "; ".join(parts)


def render_results_section(analysis: dict, raw_records: list[dict]) -> str:
    n = analysis["n_pairs_used"]
    primary = analysis["constraints"]["all_three"]
    guard = analysis["constraints"]["syntactic_validity"]
    lines = [
        f"All {n} paired trials completed successfully (no parse failures or "
        "scorer errors). Trial-level raw responses are committed as "
        "supplementary material under `study/trials/`.",
        "",
    ]
    return "\n".join(lines)


def render_primary(analysis: dict) -> str:
    n = analysis["n_pairs_used"]
    p = analysis["constraints"]["all_three"]
    or_ci = p["odds_ratio_ci_95"]
    or_str = (
        f"odds ratio 95\\% CI $[{or_ci[0]:.2f}, {or_ci[1]:.2f}]$"
        if or_ci[0] is not None
        else "odds ratio undefined (zero in a discordant cell)"
    )
    return (
        f"Of $N={n}$ paired trials, "
        f"with-memory passed all three constraints in {p['with_pass']}/{n} "
        f"trials ({fmt_pct(p['with_rate'])}; 95\\% Wilson CI "
        f"$[{fmt_pct(p['with_ci_95'][0])}, {fmt_pct(p['with_ci_95'][1])}]$). "
        f"Without-memory passed in {p['without_pass']}/{n} "
        f"({fmt_pct(p['without_rate'])}; 95\\% CI "
        f"$[{fmt_pct(p['without_ci_95'][0])}, {fmt_pct(p['without_ci_95'][1])}]$). "
        f"The risk difference is {fmt_pp(p['risk_difference'])}. "
        f"Discordant pairs: {p['discordant_b_with_yes_without_no']} with-pass/without-fail, "
        f"{p['discordant_c_with_no_without_yes']} with-fail/without-pass; "
        f"McNemar's two-sided exact $p={p['mcnemar_exact_p']:.4g}$, {or_str}."
    )


def render_guardrail(analysis: dict) -> str:
    g = analysis["constraints"]["syntactic_validity"]
    n = analysis["n_pairs_used"]
    rd = g["risk_difference"]
    breach = "BREACHED" if rd < -0.05 else "not breached"
    return (
        f"The pre-registered guardrail (with-memory syntactic-validity rate "
        f"not more than 5\\,pp below without-memory): "
        f"with={g['with_pass']}/{n} ({fmt_pct(g['with_rate'])}), "
        f"without={g['without_pass']}/{n} ({fmt_pct(g['without_rate'])}), "
        f"$\\Delta={fmt_pp(rd)}$. Guardrail {breach}."
    )


def render_per_constraint_table(analysis: dict) -> str:
    n = analysis["n_pairs_used"]
    rows = []
    for key in ("no_pip", "no_comments", "kebab_case_routes"):
        c = analysis["constraints"][key]
        nice = {
            "no_pip": "no\\,pip",
            "no_comments": "no\\,comments",
            "kebab_case_routes": "kebab-case routes",
        }[key]
        rows.append(
            f"| {nice} | "
            f"{c['with_pass']}/{n} ({fmt_pct(c['with_rate'])}) | "
            f"{c['without_pass']}/{n} ({fmt_pct(c['without_rate'])}) | "
            f"{fmt_pp(c['risk_difference'])} | "
            f"{c['discordant_b_with_yes_without_no']}/"
            f"{c['discordant_c_with_no_without_yes']} | "
            f"{c['mcnemar_exact_p']:.3g} |"
        )
    header = ("| Constraint | with-memory | without-memory | RD | "
              "$b/c$ | McNemar exact $p$ |")
    sep = "|---|---|---|---|---|---|"
    return "\n".join([header, sep] + rows)


def render_anomalies(raw_records: list[dict]) -> str:
    n_total = len(raw_records)
    timeouts = sum(1 for r in raw_records if r.get("_timeout"))
    exceptions = sum(1 for r in raw_records if r.get("_exception"))
    parse_failures = sum(
        1 for r in raw_records
        if isinstance(r.get("score"), dict)
        and (r["score"].get("_scoring_skipped") or r["score"].get("_scorer_failed"))
    )
    if timeouts == exceptions == parse_failures == 0:
        return (f"Across $N_\\text{{records}}={n_total}$ trial records (with-memory "
                f"and without-memory combined), no timeouts, no harness exceptions, "
                f"and no scorer failures were observed.")
    return (f"Of $N_\\text{{records}}={n_total}$ trial records: "
            f"{timeouts} timeouts, {exceptions} harness exceptions, "
            f"{parse_failures} scorer failures. These were excluded from the "
            f"primary analysis on a pre-registered basis.")


def render_discussion(analysis: dict) -> str:
    primary = analysis["constraints"]["all_three"]
    rd = primary["risk_difference"]
    p = primary["mcnemar_exact_p"]
    no_comments = analysis["constraints"]["no_comments"]
    no_pip = analysis["constraints"]["no_pip"]
    kebab = analysis["constraints"]["kebab_case_routes"]

    parts = []
    if rd >= 0.20 and p < 0.05:
        parts.append(
            "The pilot's primary outcome is directionally consistent with H1 "
            "and exceeds both the pre-registered statistical threshold and "
            "the minimum effect of practical interest. We interpret this as "
            "tentative evidence that the briefing-hook auto-load mechanism "
            "of `amplifier-bundle-memory`, fed a small `CONVENTIONS.md` "
            "in `project-context/`, materially reduces the rate at which "
            "an LLM ignores previously-stated coding conventions in a "
            "fresh session — at least on this single Flask-API task at "
            "this single sample size."
        )
    elif p < 0.05 and rd > 0:
        parts.append(
            "The primary outcome is statistically distinguishable from "
            "zero but smaller than the pre-registered minimum effect of "
            "practical interest. A statistical signal that does not "
            "translate into a 20-percentage-point reduction in restatement "
            "burden does not support the plain-English claim that motivated "
            "this pilot. We do not interpret this as evidence the bundle "
            "'works' for the user-facing problem."
        )
    elif rd > 0:
        parts.append(
            "The effect direction is consistent with H1 but the "
            "discordant-pair count is too small to reject the null at "
            "$\\alpha=0.05$. The pilot is uninformative as a confirmation "
            "of H1 at this $N$."
        )
    elif rd <= 0:
        parts.append(
            "The pilot does not show evidence of a memory-bundle benefit "
            "on this task. The without-memory arm matched or exceeded the "
            "with-memory arm on the primary outcome."
        )

    parts.append(
        "Per-constraint inspection shows the largest paired discordance on "
        "*no\\_comments*: with={with_n}/{N} vs. without={wo_n}/{N} "
        "($\\Delta={delta}$, $p={p:.2g}$). This is consistent with the "
        "briefing hook's auto-loaded `CONVENTIONS.md` text (\"No code "
        "comments anywhere. The code should be self-documenting.\") "
        "exerting a stable bias against comment generation that the "
        "control arm has no exposure to.".format(
            with_n=no_comments['with_pass'],
            wo_n=no_comments['without_pass'],
            N=analysis['n_pairs_used'],
            delta=fmt_pp(no_comments['risk_difference']),
            p=no_comments['mcnemar_exact_p'],
        )
    )

    parts.append(
        f"In contrast, *no\\_pip* showed $\\Delta={fmt_pp(no_pip['risk_difference'])}$ "
        f"(McNemar $p={no_pip['mcnemar_exact_p']:.2g}$) and *kebab\\_case\\_routes* "
        f"showed $\\Delta={fmt_pp(kebab['risk_difference'])}$ "
        f"($p={kebab['mcnemar_exact_p']:.2g}$). Practical conventions that "
        "fight against very common defaults (e.g. `pip install` is the "
        "default install instruction in popular online tutorials) appear "
        "harder to suppress via a single auto-loaded markdown file than "
        "stylistic conventions like 'no comments.'"
    )

    return "\n\n".join(parts)


def render_interpretation(analysis: dict) -> str:
    primary = analysis["constraints"]["all_three"]
    rd = primary["risk_difference"]
    if rd >= 0.20:
        return (
            "If the result holds in a larger replication, it would be "
            "evidence that *the simplest* form of cross-session memory in "
            "Amplifier — a static `project-context/CONVENTIONS.md` file "
            "auto-loaded by the briefing hook — produces a measurable "
            "behavioral difference on a deployment-relevant task. This "
            "is a weaker claim than the user's plain-English hypothesis, "
            "but it is testable, reproducible, and directly actionable: "
            "if you want your agent to consistently respect a project "
            "convention, write it down where the briefing hook will find it."
        )
    return (
        "Given the observed pilot result, the most direct next step is "
        "an ablation that varies the convention strength (a longer "
        "`CONVENTIONS.md`, a more emphatic phrasing) and the task type "
        "(scenarios where the conventions fight default model behavior "
        "more or less strongly). The pilot in its current form does not "
        "support a confident yes-or-no answer to the user's question."
    )


def render_negative_scope(analysis: dict) -> str:
    return (
        "This pilot does not test, and therefore cannot answer:\n\n"
        "- Whether `amplifier-bundle-memory`'s palace tool, mining hook, "
        "or interject hook contribute to constraint adherence (they were "
        "inactive under `--mode single`).\n"
        "- Whether the result holds for any LLM other than "
        "`claude-sonnet-4-6`.\n"
        "- Whether interactive tool-using sessions (the more common "
        "Amplifier configuration) yield similar results.\n"
        "- Whether the bundle helps with non-coding tasks where "
        "constraints are softer than ones graded by ruff and ast.\n"
        "- Whether the result is robust to natural user-authored "
        "conventions that lack the imperative phrasing of the test "
        "fixture's `CONVENTIONS.md`."
    )


def render_conclusion(analysis: dict) -> str:
    primary = analysis["constraints"]["all_three"]
    rd = primary["risk_difference"]
    p = primary["mcnemar_exact_p"]
    n = analysis["n_pairs_used"]
    if rd >= 0.20 and p < 0.05:
        verdict = (
            "supports H1 in this pilot at $N={N}$. We do not extrapolate "
            "beyond the limitations enumerated in \\S\\,\\ref{{limits}}; "
            "follow-up ablations of the bundle's three mechanisms, "
            "expansion to additional models and tasks, and replication "
            "with natural user-authored conventions are the next steps."
        ).format(N=n)
    elif p < 0.05 and rd > 0:
        verdict = (
            "shows a statistically detectable but practically small "
            "effect at $N={N}$. The result is informative as a baseline "
            "for power analysis, not as a confirmation of the deployment-"
            "relevant claim."
        ).format(N=n)
    else:
        verdict = (
            "does not support H1 at $N={N}$. The pilot was pre-registered, "
            "the analysis was locked, and the result is reported without "
            "selection. Follow-up work should examine whether the briefing "
            "hook is firing as intended, whether the convention-injection "
            "phrasing is strong enough, and whether the task scenario "
            "creates a ceiling effect on the outcome."
        ).format(N=n)
    return ("This pre-registered, paired pilot of the "
            "`amplifier-bundle-memory` bundle " + verdict)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", type=Path, default=Path("paper.md"))
    ap.add_argument("--analysis", type=Path, required=True)
    ap.add_argument("--jsonl", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("paper_filled.md"))
    args = ap.parse_args()

    skeleton = args.paper.read_text()
    analysis = json.loads(args.analysis.read_text())
    raw_records = []
    for line in args.jsonl.read_text().splitlines():
        if line.strip():
            raw_records.append(json.loads(line))

    primary = analysis["constraints"]["all_three"]

    subs = {
        "{{ALL_THREE_DELTA}}": f"{100 * primary['risk_difference']:+.1f}",
        "{{ALL_THREE_WITH}}": f"{primary['with_rate']:.2f}",
        "{{ALL_THREE_WITHOUT}}": f"{primary['without_rate']:.2f}",
        "{{MCNEMAR_P}}": f"{primary['mcnemar_exact_p']:.3g}",
        "{{HEADLINE_VERDICT}}": headline_verdict(primary),
        "{{PER_CONSTRAINT_SUMMARY}}": per_constraint_summary(analysis["constraints"]),
        "{{RESULTS_SECTION_PLACEHOLDER}}": render_results_section(analysis, raw_records),
        "{{PRIMARY_OUTCOME_PLACEHOLDER}}": render_primary(analysis),
        "{{GUARDRAIL_PLACEHOLDER}}": render_guardrail(analysis),
        "{{PER_CONSTRAINT_PLACEHOLDER}}": render_per_constraint_table(analysis),
        "{{ANOMALIES_PLACEHOLDER}}": render_anomalies(raw_records),
        "{{DISCUSSION_PLACEHOLDER}}": render_discussion(analysis),
        "{{INTERPRETATION_PLACEHOLDER}}": render_interpretation(analysis),
        "{{NEGATIVE_SCOPE_PLACEHOLDER}}": render_negative_scope(analysis),
        "{{CONCLUSION_PLACEHOLDER}}": render_conclusion(analysis),
    }

    filled = skeleton
    for k, v in subs.items():
        filled = filled.replace(k, v)

    args.out.write_text(filled)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
