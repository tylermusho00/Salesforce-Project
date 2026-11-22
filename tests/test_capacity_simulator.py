"""Counterfactual capacity: benefit, policy constraints, conservation and isolation."""
from copy import deepcopy

import pytest

from conftest import csv, LEAD_HEADER, REP_HEADER
from lead_router.capacity import CapacityError, compare_capacity
from lead_router.pipeline import STATE, run_pipeline_frames
import app.capacity_simulator as ui


def run(rows, reps, minimum=3):
    return run_pipeline_frames(csv(LEAD_HEADER + rows), csv(REP_HEADER + reps), min_traits=minimum)


def critical_rows(n=3):
    return "".join(f"L-{i},Acme,NA,EN,Tech,1000,Requested Live Demo,True\n" for i in range(n))


def test_capacity_relieves_critical_backlog_without_mutating_active_run():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n")
    STATE.set(r); before = deepcopy(r); run_id = STATE.run_id
    c = compare_capacity(r, ["S"], 2)
    s = c.scenarios[0]
    assert (c.baseline_critical_waiting, s["critical_waiting"], s["net_assigned"]) == (2, 0, 2)
    assert s["newly_assigned"] == 2 and s["newly_waiting"] == 0
    assert s["net_assigned_per_slot"] == 1
    assert STATE.result is r and STATE.run_id == run_id and r == before


def test_extra_junior_capacity_cannot_take_critical_leads():
    r = run(critical_rows(), "J,Jay,NA,EN,Tech,Junior,0,1\n")
    s = compare_capacity(r, ["J"], 5).scenarios[0]
    assert s["critical_waiting"] == 3 and s["net_assigned"] == 0


def test_strict_policy_gap_is_not_solved_by_more_capacity():
    r = run(critical_rows(), "S,Ana,NA,EN,Retail,Senior,0,1\n")
    s = compare_capacity(r, ["S"], 5).scenarios[0]
    assert s["review"] == 3 and s["net_assigned"] == 0


def test_scenarios_are_independent_and_rank_critical_reduction_first():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\nJ,Jay,NA,EN,Tech,Junior,0,1\n")
    c = compare_capacity(r, ["J", "S"], 1)
    assert [s["rep_id"] for s in c.scenarios] == ["S", "J"]
    assert [s["net_assigned"] for s in c.scenarios] == [1, 0]
    assert [s["new_capacity"] for s in c.scenarios] == [2, 2]
    assert c.scenarios[1]["critical_waiting"] == 2  # Senior's extra slot does not leak to Junior case


def test_unknown_industry_exception_uses_active_strict_rules():
    r = run("L,Acme,NA,EN,,1000,Requested Live Demo,True\n", "S,Ana,NA,EN,Tech,Senior,0,0\n")
    s = compare_capacity(r, ["S"], 1).scenarios[0]
    assert s["net_assigned"] == 1
    assert "Industry unknown" in s["examples"][0]["match"]


@pytest.mark.parametrize("ids,slots", [([],5),(["missing"],5),(["S","S"],5),(["S"]*4,5),
                                       (["S"],0),(["S"],51),(["S"],1.5),(["S"],True),([{}],5)])
def test_invalid_requests_are_rejected(ids, slots):
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n")
    with pytest.raises(CapacityError):
        compare_capacity(r, ids, slots)


def test_baseline_that_cannot_be_replayed_is_rejected():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n")
    r.assignments[0].rep_id = "changed-owner"
    with pytest.raises(CapacityError, match="no longer reproduce"):
        compare_capacity(r, ["S"], 1)


def test_changed_owners_and_example_limit_are_reported():
    r = run(critical_rows(30), "S,Ana,NA,EN,Tech,Senior,0,1\nM,Moe,NA,EN,Tech,Manager,0,30\n")
    s = compare_capacity(r, ["S"], 29).scenarios[0]
    assert s["net_assigned"] == 0 and s["moved_owners"] == 29
    assert s["changed_decisions"] == 29 and len(s["examples"]) == 20


def test_ui_requires_run_and_rejects_fractional_slots():
    assert "Run a dataset" in ui.simulate_capacity(1, ["S"], 5)["error"]
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n"); STATE.set(r)
    assert "whole number" in ui.simulate_capacity(1, ["S"], 1.5)["error"]


def test_ui_result_is_hidden_after_reset_or_new_run():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n"); STATE.set(r)
    data = ui.simulate_capacity(1, ["S"], 2)
    assert "comparison" in data
    STATE.reset()
    assert "Choose representatives" in ui.simulator_report(data, {}).children


def test_ui_rejects_run_replaced_while_comparing(monkeypatch):
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n"); STATE.set(r)
    real = ui.compare_capacity
    def replace_run(*args):
        value = real(*args); STATE.reset(); return value
    monkeypatch.setattr(ui, "compare_capacity", replace_run)
    assert "active run changed" in ui.simulate_capacity(1, ["S"], 2)["error"]


def test_rep_options_reset_selection_and_include_identity_and_traits():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n"); STATE.set(r)
    options, chosen = ui.simulator_options({})
    assert chosen == [] and options[0]["value"] == "S"
    assert all(text in options[0]["label"] for text in ["Ana", "Senior", "English", "Technology"])



def test_mixed_priority_replay_preserves_critical_first_not_csv_order():
    rows = ("low,Small,NA,EN,Retail,5,,False\n"
            "critical,Big,NA,EN,Retail,1000,Requested Live Demo,True\n")
    r = run(rows, "S,Ana,NA,EN,Retail,Senior,0,1\n")
    assert [a.lead_id for a in r.assignments] == ["critical", "low"]
    before = deepcopy(r)
    c = compare_capacity(r, ["S"], 1)
    assert c.scenarios[0]["net_assigned"] == 1
    assert c.scenarios[0]["critical_waiting"] == 0
    assert r == before


def test_arrival_order_run_replays_its_saved_dispatch_order():
    rows = ("low,Small,NA,EN,Retail,5,,False\n"
            "critical,Big,NA,EN,Retail,1000,Requested Live Demo,True\n")
    r = run_pipeline_frames(csv(LEAD_HEADER + rows),
        csv(REP_HEADER + "S,Ana,NA,EN,Retail,Senior,0,1\n"), min_traits=3, live=True)
    assert [a.lead_id for a in r.assignments] == ["low", "critical"]
    c = compare_capacity(r, ["S"], 1)
    assert c.baseline_critical_waiting == 1
    assert c.scenarios[0]["critical_resolved"] == 1



def test_preview_is_invalidated_when_scenario_inputs_change():
    r = run(critical_rows(), "S,Ana,NA,EN,Tech,Senior,0,1\n"); STATE.set(r)
    data = ui.simulate_capacity(1, ["S"], 2)
    assert "Selection changed" in ui.simulator_report(data, {}, ["S"], 3).children
    assert "Selection changed" in ui.simulator_report(data, {}, [], 2).children
