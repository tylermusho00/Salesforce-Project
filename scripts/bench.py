"""Median-of-N stage timings for the bundled datasets (same machine, same process).

    python scripts/bench.py [dataset=scale] [runs=5]

Prints the median seconds per stage (load, clean, score, route, metrics, total).
The CSV is read once per run, exactly like `run_dataset`; nothing is cached between runs.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lead_router.pipeline import run_dataset  # noqa: E402

STAGES = ["load", "clean", "score", "route", "metrics", "total"]


def main() -> None:
    dataset = sys.argv[1] if len(sys.argv) > 1 else "scale"
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    run_dataset(dataset)  # warm-up (imports, rules cache), not counted
    samples = [run_dataset(dataset).timings for _ in range(runs)]
    print(f"{dataset}: median of {runs} runs")
    for stage in STAGES:
        print(f"  {stage:8s} {statistics.median(t[stage] for t in samples):6.3f} s")


if __name__ == "__main__":
    main()
