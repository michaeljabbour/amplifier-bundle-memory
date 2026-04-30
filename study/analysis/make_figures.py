"""
Figure generation for the cross-session memory pilot.

Inputs:
    results_<label>.jsonl
    results_<label>.analysis.json (from analyze.py)

Outputs:
    figs/<label>_pass_rates.pdf  — primary outcome with CIs
    figs/<label>_per_constraint.pdf — per-constraint pass rates with CIs
    figs/<label>_discordant.pdf — McNemar discordant cell visualization
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import numpy as np


COLORS = {
    "with-memory": "#2a6f97",  # blue
    "without-memory": "#9d4244",  # rust
    "neutral": "#444444",
}


def load_analysis(prefix: Path) -> dict:
    p = Path(f"{prefix}.analysis.json")
    return json.loads(p.read_text())


def save(fig: plt.Figure, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def fig_pass_rates(analysis: dict, out: Path) -> None:
    primary = analysis["constraints"]["all_three"]
    n = analysis["n_pairs_used"]
    arms = ["without-memory", "with-memory"]
    rates = [primary["without_rate"], primary["with_rate"]]
    cis = [primary["without_ci_95"], primary["with_ci_95"]]
    err = [
        [r - lo for r, (lo, _) in zip(rates, cis)],
        [hi - r for r, (_, hi) in zip(rates, cis)],
    ]

    fig, ax = plt.subplots(figsize=(4.6, 3.6))
    xs = np.arange(len(arms))
    bars = ax.bar(xs, rates, yerr=err, capsize=6, color=[COLORS[a] for a in arms], width=0.55, edgecolor="black", linewidth=0.8)
    ax.set_xticks(xs)
    ax.set_xticklabels(arms)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("All-3 constraint adherence rate")
    ax.set_title(f"Primary outcome: all-three pass rate ($N={n}$ paired)\n"
                 f"McNemar exact $p={primary['mcnemar_exact_p']:.3g}$, RD={primary['risk_difference']:+.0%}",
                 fontsize=10)
    for bar, rate in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width() / 2, rate + 0.02, f"{rate:.0%}",
                ha="center", va="bottom", fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    save(fig, out)


def fig_per_constraint(analysis: dict, out: Path) -> None:
    n = analysis["n_pairs_used"]
    keys = ["no_pip", "no_comments", "kebab_case_routes", "syntactic_validity"]
    nice = {
        "no_pip": "no pip",
        "no_comments": "no comments",
        "kebab_case_routes": "kebab-case routes",
        "syntactic_validity": "syntactic validity\n(guardrail)",
    }

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    width = 0.35
    xs = np.arange(len(keys))

    with_rates = []
    without_rates = []
    with_err = [[], []]
    without_err = [[], []]
    p_values = []
    for key in keys:
        c = analysis["constraints"][key]
        with_rates.append(c["with_rate"])
        without_rates.append(c["without_rate"])
        wlo, whi = c["with_ci_95"]
        olo, ohi = c["without_ci_95"]
        with_err[0].append(c["with_rate"] - wlo)
        with_err[1].append(whi - c["with_rate"])
        without_err[0].append(c["without_rate"] - olo)
        without_err[1].append(ohi - c["without_rate"])
        p_values.append(c["mcnemar_exact_p"])

    ax.bar(xs - width / 2, without_rates, width, yerr=without_err, capsize=4,
           color=COLORS["without-memory"], edgecolor="black", linewidth=0.6,
           label="without-memory")
    ax.bar(xs + width / 2, with_rates, width, yerr=with_err, capsize=4,
           color=COLORS["with-memory"], edgecolor="black", linewidth=0.6,
           label="with-memory")
    ax.set_xticks(xs)
    ax.set_xticklabels([nice[k] for k in keys], fontsize=9)
    ax.set_ylim(0, 1.1)
    ax.set_ylabel("Pass rate")
    ax.set_title(f"Per-constraint pass rates with 95% Wilson CIs ($N={n}$)", fontsize=10)
    ax.legend(loc="upper center", ncols=2, frameon=False, fontsize=9, bbox_to_anchor=(0.5, -0.13))
    for i, p in enumerate(p_values):
        ax.text(xs[i], 1.04, f"$p={p:.2g}$", ha="center", va="bottom", fontsize=8, color=COLORS["neutral"])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    save(fig, out)


def fig_discordant(analysis: dict, out: Path) -> None:
    n = analysis["n_pairs_used"]
    primary = analysis["constraints"]["all_three"]

    a = primary["with_pass"] - primary["discordant_b_with_yes_without_no"]
    b = primary["discordant_b_with_yes_without_no"]
    c = primary["discordant_c_with_no_without_yes"]
    d = n - a - b - c

    matrix = np.array([[a, b], [c, d]])

    fig, ax = plt.subplots(figsize=(4.4, 4.0))
    im = ax.imshow(matrix, cmap="Blues", aspect="equal")
    for i in range(2):
        for j in range(2):
            ax.text(j, i, str(matrix[i, j]), ha="center", va="center",
                    color="white" if matrix[i, j] > matrix.max() / 2 else "black",
                    fontsize=14)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["with-memory: pass", "with-memory: fail"], fontsize=9)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["without-memory: pass", "without-memory: fail"], fontsize=9, rotation=90, va="center")
    ax.set_title(f"All-3 outcome 2x2 ($N={n}$)\nMcNemar exact $p={primary['mcnemar_exact_p']:.3g}$",
                 fontsize=10)
    ax.set_xlim(-0.5, 1.5)
    ax.set_ylim(1.5, -0.5)
    save(fig, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", type=Path, help="path/to/results_<label> (no extension)")
    args = ap.parse_args()
    a = load_analysis(args.prefix)
    out_dir = args.prefix.parent.parent / "figures"
    fig_pass_rates(a, out_dir / f"{args.prefix.name}_pass_rates.pdf")
    fig_per_constraint(a, out_dir / f"{args.prefix.name}_per_constraint.pdf")
    fig_discordant(a, out_dir / f"{args.prefix.name}_discordant.pdf")
    print(f"wrote figures to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
