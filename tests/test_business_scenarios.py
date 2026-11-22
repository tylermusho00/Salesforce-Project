"""
Business scenario tests: plain-language checks that Lead Router keeps its promises.

The checks themselves live in lead_router/scenarios.py, so the dashboard's "Run Tests" button and
pytest run exactly the same code. Each test asserts on the numbers a scenario produced and prints a
"RESULT:" line (run with:  pytest tests/test_business_scenarios.py -v -s).

The full write-up (why each test exists, what it showed, why that means the program works) is in
docs/TESTING.md.
"""
from __future__ import annotations

import pytest

from lead_router import scenarios
from lead_router.models import AssignmentStatus, MatchLevel, Priority


def report(scenario: scenarios.Scenario) -> None:
    """Print the scenario's result for the test log."""
    print(f"\n    RESULT: {'; '.join(scenario.result)}")


@pytest.fixture
def full(scale_run):
    """The real 50,000-lead / 1,600-rep challenge files (run once per session, see conftest.scale_run)."""
    return scale_run


def test_1_no_lead_is_ever_lost(full):
    s = scenarios.no_lead_lost(full)
    report(s)
    n = s.numbers
    assert n["lost"] == 0
    assert n["assigned"] + n["review"] == n["leads"] == n["unique_ids"] == n["outcomes"]
    assert s.passed


def test_2_urgent_leads_never_go_to_junior_reps(full):
    s = scenarios.urgent_never_to_juniors(full)
    report(s)
    assert s.numbers["junior"] == 0
    assert s.numbers["senior_share"] > 0.95
    assert s.passed


def test_2b_only_a_junior_available_means_a_manager_decides():
    s = scenarios.urgent_never_to_juniors(None)  # just the tiny case
    print(f"\n    RESULT: Critical lead + only a Junior available -> {s.numbers['tiny_status'].value}: "
          f"\"{s.numbers['tiny_reason']}\"")
    assert s.numbers["tiny_priority"] is Priority.CRITICAL
    assert s.numbers["tiny_rep"] is None and s.numbers["tiny_status"] is AssignmentStatus.MANAGER_REVIEW


def test_3_no_rep_is_ever_given_more_than_they_can_handle(full):
    s = scenarios.nobody_overloaded(full)
    report(s)
    assert s.numbers["over"] == 0
    assert s.passed


def test_3b_ten_leads_for_three_open_slots():
    s = scenarios.nobody_overloaded(None)  # just the tiny case
    print(f"\n    RESULT: 10 leads, 3 slots -> {s.numbers['tiny_assigned']} assigned, "
          f"{s.numbers['tiny_review']} in Manager Review, rep holds {s.numbers['tiny_load']} of 3.")
    assert (s.numbers["tiny_assigned"], s.numbers["tiny_review"], s.numbers["tiny_load"]) == (3, 7, 3)


def test_4_vip_referral_is_always_critical_even_for_a_tiny_company():
    s = scenarios.vip_jumps_the_line()
    report(s)
    assert s.numbers["vip_priority"] is Priority.CRITICAL and s.numbers["vip_score"] == 100
    assert s.numbers["plain_priority"] is Priority.LOW
    assert s.numbers["first"] == "L-VIP"


def test_5_messy_spellings_still_reach_the_right_rep():
    s = scenarios.messy_spellings()
    report(s)
    assert s.numbers["read_as"] == ("North America", "English", "Technology", 5000, "Requested Live Demo")
    assert s.numbers["rep"] == "Right Rep" and s.numbers["match"] is MatchLevel.EXACT


def test_6_equal_reps_get_an_equal_share():
    s = scenarios.fair_share()
    report(s)
    assert sorted(s.numbers["ours"].values()) == [10, 10, 10]
    assert max(s.numbers["naive"].values()) == 30  # what we avoid: one rep buried, two idle


def test_7_fifty_thousand_leads_in_seconds(full):
    s = scenarios.fast_upload(full)
    report(s)
    assert s.numbers["leads"] == 50_000
    assert s.numbers["seconds"] < 10


def test_8_missing_information_never_crashes_or_drops_a_lead():
    s = scenarios.missing_information()
    report(s)
    assert s.numbers["outcomes"] == 2
    assert s.numbers["partial_rep"] == "Kai"
    assert s.numbers["junk_status"] is AssignmentStatus.MANAGER_REVIEW


def test_run_all_matches_what_the_dashboard_shows(scale_run):
    """The dashboard's Tests page calls run_all(): every scenario must pass there too."""
    results, seconds = scenarios.run_all(full=scale_run)
    print(f"\n    RESULT: {sum(s.passed for s in results)} of {len(results)} scenarios passed in {seconds:.1f} s.")
    assert [s.number for s in results] == list(range(1, 9))
    assert all(s.passed for s in results)
