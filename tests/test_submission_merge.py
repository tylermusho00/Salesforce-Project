"""Integration boundaries between manual review, evidence, capacity planning and real pytest."""
from types import SimpleNamespace
import time

import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv
from app import test_runner
from lead_router.capacity import CapacityError, compare_capacity
from lead_router.models import MatchLevel
from lead_router.pipeline import PipelineState, run_pipeline_frames
from lead_router.review import ReviewError, manual_assign


def waiting_run(industry="Tech", region="NA", capacity=1):
    leads = csv(LEAD_HEADER + f"L1,Acme,{region},EN,{industry},500,Contact Sales,True\n")
    reps = csv(REP_HEADER + f"S,Sam,Europe,EN,Tech,Senior,0,{capacity}\n")
    return run_pipeline_frames(leads, reps, min_traits=3)


def test_manual_revision_preserves_evidence_and_invalidates_old_requests():
    state = PipelineState()
    original = waiting_run()
    version = state.set(original)
    _, revision = state.assign_review("L1", "S", version)
    result = state.result
    assert result is not original and original.manager_review
    assert revision == version + 1 and result.metrics["manual_assignments"] == 1
    assert result.metrics["critical_waiting"] == 0
    assert result.metrics["fingerprint"] != original.metrics["fingerprint"]
    assert result.metrics["min_traits"] == 3 and result.metrics["source_rows_preserved"] is True
    assert result.metrics["every_row_once"] is True
    with pytest.raises(ReviewError, match="active run changed"):
        state.assign_review("L1", "S", version)
    assert state.result is result and state.run_id == revision
    with pytest.raises(CapacityError, match="manual assignments"):
        compare_capacity(result, ["S"], 2)
    state.reset()
    with pytest.raises(ReviewError, match="active run changed"):
        state.assign_review("L1", "S", revision)


@pytest.mark.parametrize("industry,region,expected", [
    ("", "NA", MatchLevel.INDUSTRY_UNKNOWN),
    ("Tech", "ASIA PACIFICEurope", MatchLevel.ALTERNATE_REGION),
])
def test_manual_match_labels_retain_unknown_and_alternate_semantics(industry, region, expected):
    result = waiting_run(industry, region, capacity=0)
    assert result.manager_review
    result.reps[0].max_capacity = 1
    if not industry:
        result.reps[0].region = result.leads[0].region
    assert manual_assign(result, "L1", "S").match_level is expected


def test_review_cards_keep_the_revision_of_the_displayed_run(monkeypatch):
    import app.main as dashboard
    from lead_router.pipeline import STATE

    original = waiting_run()
    revision = STATE.set(original)
    real_choices = dashboard.rep_choices

    def replace_during_render(lead, reps):
        STATE.set(waiting_run())
        return real_choices(lead, reps)

    monkeypatch.setattr(dashboard, "rep_choices", replace_during_render)
    panel = dashboard.review_panel([], {"run_id": revision})
    context = panel[0].data
    assert context == {"run_id": revision, "lead_id": "L1"}
    with pytest.raises(ReviewError, match="active run changed"):
        STATE.assign_review(context["lead_id"], "S", context["run_id"])


def test_manual_assignment_refreshes_reserved_suggestions():
    leads = csv(LEAD_HEADER + "L1,A,NA,EN,Tech,500,Contact Sales,True\nL2,B,NA,EN,Tech,500,Contact Sales,True\n")
    reps = csv(REP_HEADER + "S,Sam,Europe,EN,Tech,Senior,0,1\n")
    result = run_pipeline_frames(leads, reps, min_traits=3)
    assert "Reassign" in result.manager_review[0].suggested_action
    manual_assign(result, "L1", "S")
    assert "Team at capacity" in result.manager_review[0].suggested_action
    assert "Reassign" not in result.manager_review[0].suggested_action


@pytest.mark.parametrize("code,xml,error", [
    (2, '<testsuites><testsuite errors="1"/></testsuites>', "code 2"),
    (0, '<testsuites/>', "no test cases"),
    (0, '<broken', "Could not read"),
    (1, '<testsuites><testcase name="test_bad"><failure message="bad"/></testcase></testsuites>', ""),
    (0, '<testsuites><testcase name="test_ok"/></testsuites>', ""),
])
def test_suite_report_cannot_hide_process_or_collection_errors(tmp_path, code, xml, error):
    report = tmp_path / "report.xml"
    report.write_text(xml)
    process = SimpleNamespace(returncode=code, communicate=lambda **kw: ("pytest output", None))
    run = test_runner.finish_suite(process, report, time.perf_counter())
    assert error in run.error if error else not run.error
    if code == 1:
        assert run.failed_cases == 1
