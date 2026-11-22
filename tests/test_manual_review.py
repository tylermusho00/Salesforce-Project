"""Manager Review workflow: suggesting the closest reps and assigning a waiting lead by hand."""
from __future__ import annotations

import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv, make_lead, make_rep
from lead_router.export import results_frame
from lead_router.models import AssignmentStatus, OverflowReason, Priority, Seniority
from lead_router.pipeline import run_pipeline_frames
from lead_router.review import ReviewError, closest_reps, manual_assign

JR, SR, MGR = Seniority.JUNIOR, Seniority.SENIOR, Seniority.MANAGER


@pytest.fixture
def waiting():
    """Two VIP (Critical) leads, one Senior slot that matches. The second lead has to wait.
        S1  Senior   NA / English / Tech     1 slot  (takes L-1)
        J1  Junior   NA / English / Tech     5 slots (not allowed: Critical)
        S2  Senior   APAC / Spanish / Retail 2 slots (shares nothing)
        M1  Manager  Europe / German / Fin.  1 slot  (shares nothing)
    """
    leads = csv(LEAD_HEADER + "L-1,Acme,NA,EN,Tech,500,Contact Sales,True\n"
                              "L-2,Bolt,NA,EN,Tech,500,Contact Sales,True\n")
    reps = csv(REP_HEADER + "S1,Sam,NA,EN,Tech,Senior,0,1\n"
                            "J1,Jo,NA,EN,Tech,Junior,0,5\n"
                            "S2,Sue,APAC,ES,Retail,Senior,0,2\n"
                            "M1,Max,Europe,DE,Finance,Manager,0,1\n")
    result = run_pipeline_frames(leads, reps)
    assert [a.lead_id for a in result.manager_review] == ["L-2"]
    assert result.manager_review[0].overflow_reason is OverflowReason.ALL_AT_CAPACITY
    return result


def lead(result, lead_id):
    return next(l for l in result.leads if l.id == lead_id)


# ---------------------------------------------------------------------- suggestions
def test_closest_reps_most_traits_first_and_skips_full_reps():
    """3 traits beats 2; equal reps keep file order; a full rep is never suggested."""
    reps = [make_rep("A", JR, region="North America", industry="Technology"),   # 2 of 3 (region, language)
            make_rep("B", SR, region="Europe", industry="Retail"),              # 2 of 3 (language, industry)
            make_rep("C", MGR, industry="Retail"),                              # 3 of 3
            make_rep("D", SR, industry="Retail", max_capacity=2, current=2)]    # 3 of 3 but full
    medium = make_lead("L", industry="Retail", priority=Priority.MEDIUM)
    picks = closest_reps(medium, reps)
    assert [c.rep.id for c in picks] == ["C", "A", "B"]
    assert [c.traits for c in picks] == [3, 2, 2]


def test_closest_reps_for_a_critical_lead_never_include_juniors(waiting):
    picks = closest_reps(lead(waiting, "L-2"), waiting.reps)
    ids = [c.rep.id for c in picks]
    assert "J1" not in ids and "S1" not in ids          # Junior not allowed, Senior full
    assert ids == ["S2", "M1"]                           # equal (0 traits): Senior before Manager


def test_closest_reps_say_which_traits_match():
    rep = make_rep("A", region="North America", language="Spanish", industry="Technology")
    pick = closest_reps(make_lead("L"), [rep])[0]
    assert pick.matched == ("region", "industry") and pick.missed == ("language",)


# ---------------------------------------------------------------------- assigning by hand
def test_manager_assignment_moves_the_lead_and_updates_every_number(waiting):
    before = waiting.metrics["overflow"]
    decision = manual_assign(waiting, "L-2", "S2")
    assert decision.status is AssignmentStatus.ASSIGNED and decision.manual and decision.rep_id == "S2"
    assert waiting.manager_review == []
    assert next(r for r in waiting.reps if r.id == "S2").current_capacity == 1
    m = waiting.metrics
    assert (before, m["overflow"], m["assigned"], m["manual_assignments"]) == (1, 0, 2, 1)
    assert m["junior_critical_violations"] == 0 and m["capacity_violations"] == 0
    row = results_frame(waiting).set_index("lead_id").loc["L-2"]
    assert row["assigned_by"] == "Manager" and "Assigned by a manager" in row["reason"]


def test_manager_cannot_give_a_critical_lead_to_a_junior(waiting):
    with pytest.raises(ReviewError, match="Juniors can't take Critical"):
        manual_assign(waiting, "L-2", "J1")
    assert waiting.metrics["overflow"] == 1             # nothing changed


def test_manager_cannot_overfill_a_rep(waiting):
    with pytest.raises(ReviewError, match="is full"):
        manual_assign(waiting, "L-2", "S1")


def test_only_waiting_leads_can_be_assigned_by_hand(waiting):
    with pytest.raises(ReviewError, match="isn't waiting"):
        manual_assign(waiting, "L-1", "S2")
    with pytest.raises(ReviewError, match="isn't on the team"):
        manual_assign(waiting, "L-2", "NOBODY")
