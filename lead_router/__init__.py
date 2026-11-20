"""
Lead Router: clean, score and route sales leads to the right rep.

Files, in the order data flows through them:
    config.py    loads config/rules.yaml (all business numbers)
    models.py    data shapes: Lead, Rep, Assignment, PipelineResult
    cleaning.py  STEP 1  messy CSV rows -> Lead / Rep objects
    scoring.py   STEP 2  points, tiers, VIP override
    routing.py   STEP 3  match leads to reps (the core algorithm)
    pipeline.py  STEP 4  runs 1-3, measures time, computes KPIs, keeps state
    export.py    results -> tables / CSV
    cli.py       `leadrouter run --dataset scale`
"""

from .pipeline import PipelineError, reset_state, run_dataset, run_pipeline

__version__ = "0.2.0"
__all__ = ["PipelineError", "reset_state", "run_dataset", "run_pipeline"]
