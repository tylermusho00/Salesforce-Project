"""
cli.py - run the pipeline from a terminal.

    leadrouter run                               # sample dataset
    leadrouter run --dataset scale               # 50,000 leads
    leadrouter run --dataset scale --out results.csv
    leadrouter run --leads my_leads.csv --reps my_reps.csv
    leadrouter run --dataset scale --strategy round_robin   # try a different distribution method
    leadrouter compare --dataset scale                       # all distribution methods side by side

(`python -m lead_router run ...` does the same thing.)
Uses argparse from the standard library, so there is nothing extra to install.
"""
from __future__ import annotations

import argparse
import sys

from .distribution import DESCRIPTIONS, STRATEGIES
from .export import results_frame
from .models import PipelineResult
from .pipeline import PipelineError, run_dataset, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leadrouter", description="Clean, score and route leads to reps.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run the full pipeline and print the KPIs.")
    run.add_argument("--dataset", choices=["sample", "scale"], default="sample",
                     help="Bundled dataset to run (default: sample).")
    run.add_argument("--leads", help="Your own leads CSV (use together with --reps).")
    run.add_argument("--reps", help="Your own reps CSV (use together with --leads).")
    run.add_argument("--out", help="Also save the routed leads to this CSV file.")
    run.add_argument("--strategy", choices=list(STRATEGIES), help="Distribution method (default: rules.yaml).")

    compare = commands.add_parser("compare", help="Run every distribution strategy and compare fairness.")
    compare.add_argument("--dataset", choices=["sample", "scale"], default="scale")
    return parser


def print_summary(result: PipelineResult) -> None:
    m, t = result.metrics, result.timings
    rows = [
        ("Leads processed", f"{m['total_leads']:,}"),
        ("Assigned", f"{m['assigned']:,} ({m['assigned_rate']}%)"),
        ("Manager Review", f"{m['overflow']:,} ({m['overflow_rate']}%)"),
        ("Tiers", ", ".join(f"{tier} {count:,}" for tier, count in m["tiers"].items())),
        ("Critical -> Senior", f"{m['critical_to_senior']:,} of {m['critical_total']:,} ({m['critical_senior_rate']}%)"),
        ("Critical -> Manager", f"{m['critical_to_manager']:,}"),
        ("Critical given to a Junior", f"{m['junior_critical_violations']}  (must be 0)"),
        ("Reps over capacity", f"{m['capacity_violations']}  (must be 0)"),
        ("Leads with imputed data", f"{m['imputed_rate']}%"),
        ("Team utilization", f"{m['utilization']}%"),
        ("Distribution", f"{m['strategy']} · fairness (Jain index) {m['fairness_during_run']:.4f} workload, "
                         f"{m['value_fairness']:.4f} value"),
        ("Match quality", ", ".join(f"{k}: {v:,}" for k, v in m["match_levels"].items()) or "-"),
        ("Overflow reasons", ", ".join(f"{k}: {v:,}" for k, v in m["overflow_reasons"].items()) or "-"),
        ("Throughput", f"{m['throughput_lps']:,} leads/second"),
        ("Time per stage", ", ".join(f"{stage} {secs:.2f}s" for stage, secs in t.items())),
    ]
    width = max(len(label) for label, _ in rows)
    print(f"\nLead Router - {result.dataset}\n" + "-" * 60)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")
    print()


def compare_strategies(dataset: str) -> None:
    """Run the same data through every distribution strategy, in two arrival modes."""
    for live in (False, True):
        _compare_table(dataset, live)
    print("All fairness numbers are Jain's index within groups of interchangeable reps (1.0 = perfectly even).")
    print("  Workload = % of capacity used, averaged over 10 snapshots taken while leads were handed out")
    print("  At 10%   = workload fairness after the first 10% of leads")
    print("  Value    = lead value (sum of scores) received per slot, at the end\n")


def _compare_table(dataset: str, live: bool) -> None:
    mode = ("LIVE: leads routed in the order they arrive (mixed priorities)" if live
            else "BATCH: leads routed Critical-first (how the pipeline runs)")
    header = (f"{'Strategy':<14}{'What it does':<42}{'Workload':>10}{'At 10%':>8}{'Value':>8}"
              f"{'Assigned':>10}{'Crit->Sr':>10}")
    print(f"\n{dataset} dataset - {mode}")
    print(header)
    print("-" * len(header))
    for name in STRATEGIES:
        m = run_dataset(dataset, strategy=name, live=live).metrics
        timeline = m["fairness_timeline"] or [1.0]
        print(f"{name:<14}{DESCRIPTIONS[name]:<42}{m['fairness_during_run']:>10.4f}{timeline[0]:>8.4f}"
              f"{m['value_fairness']:>8.4f}{m['assigned_rate']:>9.2f}%{m['critical_senior_rate']:>9.2f}%")
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compare":
            compare_strategies(args.dataset)
            return 0
        if args.leads or args.reps:
            if not (args.leads and args.reps):
                raise PipelineError("Pass both --leads and --reps.")
            result = run_pipeline(args.leads, args.reps, strategy=args.strategy)
        else:
            result = run_dataset(args.dataset, strategy=args.strategy)
    except PipelineError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print_summary(result)
    if args.out:
        results_frame(result).to_csv(args.out, index=False)
        print(f"Saved routed leads to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
