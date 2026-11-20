"""
pipeline.py - STEP 4: run everything in order and measure it.

    leads.csv + reps.csv
        │  read_csv()      check the files exist, aren't empty, have the right columns
        ▼
    clean_leads() / clean_reps()        cleaning.py
        ▼
    score_all()                          scoring.py
        ▼
    route_all()                          routing.py
        ▼
    compute_metrics()                    KPIs for the dashboard
        ▼
    PipelineResult  (also kept in STATE so the dashboard can read it and Reset can clear it)

Every stage is timed, so we can report e.g. "50,000 leads in 1.9 s".
Bad input raises PipelineError with a plain-English message instead of a Python traceback.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .cleaning import (LEAD_COLUMNS, LEAD_REQUIRED, REP_COLUMNS, REP_REQUIRED, clean_leads,
                       clean_reps, missing_columns, standardize_columns)
from .config import dataset_paths, load_rules
from .distribution import DEFAULT_STRATEGY, distribution_fairness
from .models import Assignment, AssignmentStatus, Lead, PipelineResult, Priority, Rep, Seniority
from .routing import route_all
from .scoring import score_all


class PipelineError(Exception):
    """A problem with the input that the user can fix (bad path, empty file, missing columns)."""


# ======================================================================================
# Loading
# ======================================================================================
def read_csv(source: str | Path | Any, label: str, expected: list[str], required: list[str]) -> pd.DataFrame:
    """Read a CSV with every column as text (cleaning decides the real types) and check its header.

    `source` can be a file path or an open file-like object (used for dashboard uploads).
    `label` ("Leads" / "Reps") is only used in error messages.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise PipelineError(f"{label} file not found: {path}")
        if path.stat().st_size == 0:
            raise PipelineError(f"{label} file is empty: {path.name}")

    try:
        # keep_default_na=False: keep "NA" as text ("NA" is North America here, not "missing")
        df = pd.read_csv(source, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        raise PipelineError(f"{label} file is empty.") from None
    except (UnicodeDecodeError, pd.errors.ParserError) as error:
        raise PipelineError(f"{label} file is not a readable CSV: {error}") from None

    df = standardize_columns(df, expected)
    missing = missing_columns(df, required)
    if missing:
        raise PipelineError(f"{label} file is missing required column(s): {', '.join(missing)}. "
                            f"Found: {', '.join(map(str, df.columns))}")
    return df


# ======================================================================================
# Metrics
# ======================================================================================
def percent(part: float, whole: float) -> float:
    """Percentage rounded to 2 decimals; 0 when the whole is 0 (avoids dividing by zero)."""
    return round(100 * part / whole, 2) if whole else 0.0


def compute_metrics(leads: list[Lead], reps: list[Rep], assignments: list[Assignment],
                    elapsed_seconds: float) -> dict[str, Any]:
    """Every number the dashboard and CLI show. Rates are percentages (0-100).

    The two *_violations numbers are safety checks. They should always be 0.
    """
    total = len(assignments)
    assigned = [a for a in assignments if a.status is AssignmentStatus.ASSIGNED]
    overflow_count = total - len(assigned)

    # Critical leads: who handled them?
    critical = [a for a in assignments if a.priority is Priority.CRITICAL]
    critical_by_seniority = Counter(a.rep_seniority for a in critical)
    to_senior = critical_by_seniority[Seniority.SENIOR]
    to_manager = critical_by_seniority[Seniority.MANAGER]

    # Data quality
    leads_with_guesses = sum(1 for lead in leads if lead.imputed_fields)

    # Capacity
    total_capacity = sum(rep.max_capacity for rep in reps)
    total_load = sum(rep.current_capacity for rep in reps)

    tier_counts = Counter(a.priority for a in assignments)
    return {
        # Volume
        "total_leads": total,
        "assigned": len(assigned),
        "overflow": overflow_count,
        "assigned_rate": percent(len(assigned), total),
        "overflow_rate": percent(overflow_count, total),
        "tiers": {p.value: tier_counts[p] for p in Priority},
        "overflow_reasons": dict(Counter(a.overflow_reason.value for a in assignments if a.overflow_reason)),
        "match_levels": dict(Counter(a.match_level.value for a in assigned if a.match_level)),
        # Critical handling
        "critical_total": len(critical),
        "critical_to_senior": to_senior,
        "critical_to_manager": to_manager,
        "critical_overflow": len(critical) - to_senior - to_manager,
        "critical_senior_rate": percent(to_senior, len(critical)),
        "critical_covered_rate": percent(to_senior + to_manager, len(critical)),
        # Safety checks (must be 0)
        "junior_critical_violations": critical_by_seniority[Seniority.JUNIOR],
        "capacity_violations": sum(1 for rep in reps if rep.current_capacity > rep.max_capacity),
        # Data quality
        "imputed_leads": leads_with_guesses,
        "imputed_rate": percent(leads_with_guesses, total),
        # Team capacity
        "reps_total": len(reps),
        "reps_full": sum(1 for rep in reps if rep.max_capacity and rep.is_full),
        "reps_idle": sum(1 for rep in reps if rep.current_capacity == 0),
        "capacity_total": total_capacity,
        "load_total": total_load,
        "utilization": percent(total_load, total_capacity),
        "rep_utilization": {rep.id: percent(rep.current_capacity, rep.max_capacity) for rep in reps},
        # Distribution fairness: Jain's index within groups of interchangeable reps (1.0 = perfectly even)
        "distribution_fairness": distribution_fairness(reps),
        # Speed
        "throughput_lps": round(total / elapsed_seconds) if elapsed_seconds > 0 else 0,
    }


# ======================================================================================
# Running the pipeline
# ======================================================================================
@contextmanager
def timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    """Usage:  with timed(timings, "clean"): ...   -> timings["clean"] = seconds taken."""
    start = time.perf_counter()
    yield
    timings[stage] = time.perf_counter() - start


def run_pipeline_frames(leads_df: pd.DataFrame, reps_df: pd.DataFrame, dataset: str = "custom",
                        timings: dict[str, float] | None = None, strategy: str | None = None,
                        live: bool = False) -> PipelineResult:
    """Run clean -> score -> route -> metrics on DataFrames that are already loaded.
    `strategy` overrides the distribution method from rules.yaml (see distribution.py)."""
    timings = dict(timings or {})
    strategy = strategy or load_rules()["routing"].get("distribution_strategy") or DEFAULT_STRATEGY

    with timed(timings, "clean"):
        leads, lead_quality = clean_leads(leads_df)
        reps, rep_quality = clean_reps(reps_df)

    with timed(timings, "score"):
        leads = score_all(leads)

    with timed(timings, "route"):
        routed = route_all(leads, reps, strategy, live)

    seconds_so_far = sum(timings.values())
    with timed(timings, "metrics"):
        metrics = compute_metrics(leads, routed.reps, routed.assignments, seconds_so_far)
        metrics["strategy"] = strategy
        timeline = routed.fairness_timeline
        metrics["fairness_timeline"] = timeline
        # average fairness while leads were being handed out (not just at the end)
        metrics["fairness_during_run"] = round(sum(timeline) / len(timeline), 4) if timeline else 1.0
        metrics["value_fairness"] = routed.value_fairness
    timings["total"] = sum(timings.values())

    if not reps:
        lead_quality.warnings.append("No representatives loaded: every lead went to Manager Review.")
    if not leads:
        lead_quality.warnings.append("No leads in the file.")

    return PipelineResult(dataset=dataset, leads=leads, reps=routed.reps,
                          assignments=routed.assignments, manager_review=routed.overflow,
                          metrics=metrics, timings=timings,
                          lead_quality=lead_quality, rep_quality=rep_quality)


def run_pipeline(leads_path: str | Path | Any, reps_path: str | Path | Any,
                 dataset: str = "custom", strategy: str | None = None, live: bool = False) -> PipelineResult:
    """Main entry point: read two CSVs and run every stage."""
    timings: dict[str, float] = {}
    with timed(timings, "load"):
        leads_df = read_csv(leads_path, "Leads", LEAD_COLUMNS, LEAD_REQUIRED)
        reps_df = read_csv(reps_path, "Reps", REP_COLUMNS, REP_REQUIRED)
    return run_pipeline_frames(leads_df, reps_df, dataset, timings, strategy, live)


def run_dataset(name: str, strategy: str | None = None, live: bool = False) -> PipelineResult:
    """Run one of the bundled datasets from rules.yaml ("sample" or "scale")."""
    leads_path, reps_path = dataset_paths(name)
    return run_pipeline(leads_path, reps_path, dataset=name, strategy=strategy, live=live)


# ======================================================================================
# In-memory state (no database needed)
# ======================================================================================
class PipelineState:
    """Holds the latest result so the dashboard can read it and Reset can clear it.

    run_id goes up on every run or reset, which tells the dashboard to redraw.
    The lock stops two simultaneous button clicks from overwriting each other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.result: PipelineResult | None = None
        self.run_id = 0

    def set(self, result: PipelineResult) -> int:
        with self._lock:
            self.result = result
            self.run_id += 1
            return self.run_id

    def reset(self) -> None:
        """Forget the results and put every rep's load back to 0."""
        with self._lock:
            if self.result is not None:
                for rep in self.result.reps:
                    rep.current_capacity = 0
            self.result = None
            self.run_id += 1


STATE = PipelineState()


def reset_state() -> None:
    """Clear outputs and set every rep's capacity back to 0 (baseline)."""
    STATE.reset()
