"""Scale benchmark: 50,000 leads x 1,600 reps must finish under the target time.

Run alone with:  pytest tests/test_benchmark.py --benchmark-only
"""
from lead_router.pipeline import run_pipeline

TARGET_SECONDS = 10.0  # recorded result goes in the README / slides


def test_scale_dataset_under_target(benchmark, scale_paths):
    result = benchmark.pedantic(run_pipeline, args=scale_paths, rounds=3, iterations=1)
    m = result.metrics
    assert m["total_leads"] == 50_000
    assert m["assigned"] + m["overflow"] == m["total_leads"]
    assert m["junior_critical_violations"] == 0 and m["capacity_violations"] == 0
    # The pipeline times itself, so this check also works with --benchmark-disable (as in CI).
    assert result.timings["total"] < TARGET_SECONDS
