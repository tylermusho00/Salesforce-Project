"""
cli.py - run the pipeline from a terminal.

    leadrouter run                               # sample dataset
    leadrouter run --dataset scale               # 50,000 leads
    leadrouter run --dataset scale --out results.csv
    leadrouter run --leads my_leads.csv --reps my_reps.csv

(`python -m lead_router run ...` does the same thing.)
Uses argparse from the standard library, so there is nothing extra to install.
"""
from __future__ import annotations

import argparse
import sys

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
        ("Overflow reasons", ", ".join(f"{k}: {v:,}" for k, v in m["overflow_reasons"].items()) or "-"),
        ("Throughput", f"{m['throughput_lps']:,} leads/second"),
        ("Time per stage", ", ".join(f"{stage} {secs:.2f}s" for stage, secs in t.items())),
    ]
    width = max(len(label) for label, _ in rows)
    print(f"\nLead Router - {result.dataset}\n" + "-" * 60)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.leads or args.reps:
            if not (args.leads and args.reps):
                raise PipelineError("Pass both --leads and --reps.")
            result = run_pipeline(args.leads, args.reps)
        else:
            result = run_dataset(args.dataset)
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
