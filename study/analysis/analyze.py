"""
Analysis driver for the cross-session memory pilot.

Inputs:  results_<label>.jsonl produced by run_pilot.py
Outputs: analysis_<label>.json (numerical summary)
         figs/effect.pdf, figs/per_constraint.pdf (matplotlib)
         analysis_<label>.md (human-readable report)

Stat tests:
  - Primary: McNemar's exact test on all-three outcome
  - Per-arm: Wilson 95% CI on each arm's pass rate
  - Per-constraint: paired discordant cell counts + descriptive proportions
  - Guardrail: syntactic validity rate per arm
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers (no scipy dependency to keep the harness self-contained)
# ---------------------------------------------------------------------------


def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.959963984540054  # 0.975 quantile of N(0,1)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact (mid-P-style not used; classic exact) McNemar p-value.

    H0: pr(arm1>arm2 | discordant) = 0.5.  Test statistic is the count in
    one of the discordant cells under Binomial(n=b+c, p=0.5).  Two-sided
    p = sum of binomial PMF values <= observed (most extreme).
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # Cumulative probability of <= k under Binomial(n, 0.5)
    # P(X <= k) = sum_{i=0..k} C(n,i) * 0.5^n
    log_half_n = -n * math.log(2)
    log_pmf_terms = []
    for i in range(0, k + 1):
        log_pmf_terms.append(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + log_half_n)
    # Two-sided: by symmetry double the lower-tail probability, capped at 1.
    p_one = sum(math.exp(t) for t in log_pmf_terms)
    return min(1.0, 2 * p_one)


def or_exact_ci_discordant(b: int, c: int, alpha: float = 0.05) -> tuple[float, float] | tuple[None, None]:
    """Exact 1-α CI for the odds ratio in the McNemar setting.

    Equivalent to the Clopper-Pearson interval on b / (b + c) mapped through
    p / (1-p).  When b+c = 0, the OR is undefined.
    """
    n = b + c
    if n == 0 or b == 0 or c == 0:
        # Indeterminate or extreme — return None
        return (None, None)

    # Clopper-Pearson on b/(b+c)
    from math import lgamma

    def cp_lo(k: int, nn: int, a: float) -> float:
        # numeric incomplete-beta inversion via bisection
        if k == 0:
            return 0.0
        lo, hi = 0.0, k / nn
        for _ in range(100):
            mid = (lo + hi) / 2
            if cdf_geq(k, nn, mid) < a / 2:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def cp_hi(k: int, nn: int, a: float) -> float:
        if k == nn:
            return 1.0
        lo, hi = k / nn, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if cdf_leq(k, nn, mid) < a / 2:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2

    def cdf_leq(k: int, nn: int, p: float) -> float:
        s = 0.0
        for i in range(0, k + 1):
            s += math.exp(lgamma(nn + 1) - lgamma(i + 1) - lgamma(nn - i + 1) + i * math.log(p) + (nn - i) * math.log(1 - p))
        return s

    def cdf_geq(k: int, nn: int, p: float) -> float:
        return 1 - cdf_leq(k - 1, nn, p) if k > 0 else 1.0

    pi_lo = cp_lo(b, n, alpha)
    pi_hi = cp_hi(b, n, alpha)
    or_lo = pi_lo / (1 - pi_lo) if pi_lo < 1 else float("inf")
    or_hi = pi_hi / (1 - pi_hi) if pi_hi < 1 else float("inf")
    return (or_lo, or_hi)


# ---------------------------------------------------------------------------
# Pair extraction
# ---------------------------------------------------------------------------


@dataclass
class PairedResult:
    trial_index: int
    with_score: dict[str, Any] | None
    without_score: dict[str, Any] | None

    def both_present(self) -> bool:
        return self.with_score is not None and self.without_score is not None

    def with_pass(self, key: str) -> bool | None:
        if self.with_score is None:
            return None
        return bool(self.with_score.get(key))

    def without_pass(self, key: str) -> bool | None:
        if self.without_score is None:
            return None
        return bool(self.without_score.get(key))


def load_pairs(jsonl: Path) -> list[PairedResult]:
    by_index: dict[int, PairedResult] = {}
    for line in jsonl.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        tid = rec.get("trial_id", "")
        # trial_id format: "<label>_<NNN>_<arm>"
        try:
            idx = int(tid.rsplit("_", 2)[1])
        except (ValueError, IndexError):
            continue
        arm = rec.get("arm")
        score = rec.get("score") if isinstance(rec.get("score"), dict) else None
        if score is None or "_scoring_skipped" in score or "_scorer_failed" in score:
            score = None
        pair = by_index.setdefault(idx, PairedResult(idx, None, None))
        if arm == "with-memory":
            pair.with_score = score
        elif arm == "without-memory":
            pair.without_score = score
    return [by_index[k] for k in sorted(by_index)]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class ConstraintAnalysis:
    name: str
    with_pass: int = 0
    with_fail: int = 0
    without_pass: int = 0
    without_fail: int = 0
    discordant_with_yes_without_no: int = 0  # cell b
    discordant_with_no_without_yes: int = 0  # cell c
    pairs: int = 0

    def add(self, with_p: bool, without_p: bool) -> None:
        self.pairs += 1
        if with_p:
            self.with_pass += 1
        else:
            self.with_fail += 1
        if without_p:
            self.without_pass += 1
        else:
            self.without_fail += 1
        if with_p and not without_p:
            self.discordant_with_yes_without_no += 1
        if (not with_p) and without_p:
            self.discordant_with_no_without_yes += 1

    def report(self) -> dict[str, Any]:
        with_p = self.with_pass / self.pairs if self.pairs else 0.0
        without_p = self.without_pass / self.pairs if self.pairs else 0.0
        rd = with_p - without_p
        b = self.discordant_with_yes_without_no
        c = self.discordant_with_no_without_yes
        with_lo, with_hi = wilson_ci(self.with_pass, self.pairs)
        without_lo, without_hi = wilson_ci(self.without_pass, self.pairs)
        p_value = mcnemar_exact_p(b, c)
        or_lo, or_hi = or_exact_ci_discordant(b, c)
        return {
            "name": self.name,
            "n_pairs": self.pairs,
            "with_pass": self.with_pass,
            "with_rate": with_p,
            "with_ci_95": [with_lo, with_hi],
            "without_pass": self.without_pass,
            "without_rate": without_p,
            "without_ci_95": [without_lo, without_hi],
            "risk_difference": rd,
            "discordant_b_with_yes_without_no": b,
            "discordant_c_with_no_without_yes": c,
            "mcnemar_exact_p": p_value,
            "odds_ratio_ci_95": [or_lo, or_hi],
        }


def analyze(jsonl: Path) -> dict[str, Any]:
    pairs = load_pairs(jsonl)
    pairs = [p for p in pairs if p.both_present()]
    n = len(pairs)

    constraint_keys = {
        "all_three": "All-3 constraint adherence (primary)",
        "no_pip": "No pip / requirements.txt",
        "no_comments": "No comments / docstrings",
        "kebab_case_routes": "Kebab-case routes",
        "syntactic_validity": "Syntactic validity (guardrail)",
    }

    analyses: dict[str, ConstraintAnalysis] = {
        k: ConstraintAnalysis(name=v) for k, v in constraint_keys.items()
    }

    for pair in pairs:
        for k in constraint_keys:
            with_p = pair.with_pass(k)
            without_p = pair.without_pass(k)
            if with_p is None or without_p is None:
                continue
            analyses[k].add(with_p, without_p)

    return {
        "n_pairs_total_in_file": len(load_pairs(jsonl)),
        "n_pairs_used": n,
        "constraints": {k: v.report() for k, v in analyses.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("--out-prefix", type=Path, default=None)
    args = ap.parse_args()

    if not args.jsonl.exists():
        print(f"missing: {args.jsonl}", file=sys.stderr)
        return 2

    result = analyze(args.jsonl)
    out_prefix = args.out_prefix or args.jsonl.with_suffix("")
    json_out = Path(f"{out_prefix}.analysis.json")
    json_out.write_text(json.dumps(result, indent=2))
    print(f"wrote {json_out}")

    md_out = Path(f"{out_prefix}.analysis.md")
    md_out.write_text(render_markdown(result, json_out.name))
    print(f"wrote {md_out}")
    return 0


def render_markdown(result: dict[str, Any], src: str) -> str:
    n = result["n_pairs_used"]
    lines = [f"# Analysis ({src})", "", f"Pairs analyzed: **{n}** (paired, both arms present).", ""]
    primary = result["constraints"]["all_three"]
    lines += [
        "## Primary outcome — all 3 constraints",
        "",
        f"- with-memory: **{primary['with_pass']}/{n}** = {primary['with_rate']:.1%} "
        f"(95% CI {primary['with_ci_95'][0]:.1%}\u2013{primary['with_ci_95'][1]:.1%})",
        f"- without-memory: **{primary['without_pass']}/{n}** = {primary['without_rate']:.1%} "
        f"(95% CI {primary['without_ci_95'][0]:.1%}\u2013{primary['without_ci_95'][1]:.1%})",
        f"- risk difference: **{primary['risk_difference']:+.1%}**",
        f"- discordant cells (with+/without\u2212, with\u2212/without+): "
        f"({primary['discordant_b_with_yes_without_no']}, {primary['discordant_c_with_no_without_yes']})",
        f"- McNemar exact p-value: **{primary['mcnemar_exact_p']:.4g}**",
    ]
    or_ci = primary["odds_ratio_ci_95"]
    if or_ci[0] is not None:
        lines.append(f"- odds ratio 95% CI: ({or_ci[0]:.2f}, {or_ci[1]:.2f})")
    lines += ["", "## Per-constraint and guardrail", "",
              "| Constraint | with-memory | without-memory | RD | b/c | McNemar p |",
              "|---|---|---|---|---|---|"]
    for key, c in result["constraints"].items():
        if key == "all_three":
            continue
        row = (f"| {c['name']} | {c['with_pass']}/{n} ({c['with_rate']:.0%}) "
               f"| {c['without_pass']}/{n} ({c['without_rate']:.0%}) "
               f"| {c['risk_difference']:+.0%} "
               f"| {c['discordant_b_with_yes_without_no']}/{c['discordant_c_with_no_without_yes']} "
               f"| {c['mcnemar_exact_p']:.3f} |")
        lines.append(row)
    lines += ["", "_Generated by `analyze.py`._"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
