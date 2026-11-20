import io

import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv
from lead_router.pipeline import STATE, PipelineError, reset_state, run_pipeline, run_pipeline_frames

ONE_LEAD = LEAD_HEADER + "L-1,Acme,NA,EN,Technology,1000,Requested Live Demo,False\n"
ONE_REP = REP_HEADER + "R-1,Ann,North America,English,Technology,Senior,0,22\n"


def test_empty_leads_file():
    r = run_pipeline_frames(csv(LEAD_HEADER), csv(ONE_REP))
    assert r.metrics["total_leads"] == 0 and r.assignments == []
    assert "No leads" in " ".join(r.lead_quality.warnings)


def test_one_lead_one_rep():
    r = run_pipeline_frames(csv(ONE_LEAD), csv(ONE_REP))
    m = r.metrics
    assert (m["assigned"], m["overflow"], m["critical_to_senior"]) == (1, 0, 1)
    assert r.reps[0].current_capacity == 1


def test_zero_reps_sends_everything_to_review():
    r = run_pipeline_frames(csv(ONE_LEAD), csv(REP_HEADER))
    assert r.metrics["overflow"] == 1 and len(r.manager_review) == 1
    assert "No representatives" in " ".join(r.lead_quality.warnings)


def test_reset_restores_capacities_to_zero():
    r = run_pipeline_frames(csv(ONE_LEAD), csv(ONE_REP))
    STATE.set(r)
    assert r.reps[0].current_capacity == 1
    reset_state()
    assert r.reps[0].current_capacity == 0 and STATE.result is None


def test_metrics_are_consistent():
    leads = LEAD_HEADER + "".join(f"L-{i},Co,NA,EN,Technology,{i * 100},Contact Sales,False\n" for i in range(30))
    r = run_pipeline_frames(csv(leads), csv(ONE_REP))
    m = r.metrics
    assert m["assigned"] + m["overflow"] == m["total_leads"] == 30
    assert m["assigned"] == 22 and m["capacity_violations"] == 0 and m["junior_critical_violations"] == 0
    assert sum(m["tiers"].values()) == 30


@pytest.mark.parametrize("leads,reps,message", [
    ("missing.csv", "missing.csv", "not found"),
    (io.StringIO(""), io.StringIO(ONE_REP), "empty"),
    (io.StringIO("Lead_ID,Region\nL-1,NA\n"), io.StringIO(ONE_REP), "missing required column"),
    (io.StringIO(ONE_LEAD), io.StringIO("Rep_ID\nR-1\n"), "missing required column"),
])
def test_bad_inputs_raise_clear_errors(leads, reps, message):
    with pytest.raises(PipelineError, match=message):
        run_pipeline(leads, reps)


def test_empty_file_on_disk(tmp_path):
    (tmp_path / "leads.csv").write_text("")
    (tmp_path / "reps.csv").write_text(ONE_REP)
    with pytest.raises(PipelineError, match="empty"):
        run_pipeline(tmp_path / "leads.csv", tmp_path / "reps.csv")


def test_sample_dataset_end_to_end():
    from lead_router.pipeline import run_dataset
    try:
        r = run_dataset("sample")
    except PipelineError:
        pytest.skip("sample dataset not present in data/")
    m = r.metrics
    assert m["total_leads"] + r.lead_quality.duplicates_removed == r.lead_quality.rows_in == 100
    assert m["assigned"] + m["overflow"] == m["total_leads"]
    assert m["junior_critical_violations"] == 0 and m["capacity_violations"] == 0
