"""
Business scenario tests: plain-language checks that Lead Router keeps its promises.

Each test is one promise a sales manager cares about. It runs the WHOLE program (read CSV -> clean ->
score -> route), not just one function, and prints a "RESULT:" line so the outcome can be read in
the test log (run with:  pytest tests/test_business_scenarios.py -v -s).

The full write-up (why each test exists, what it showed, why that means the program works) is in
docs/TESTING.md.
"""
from __future__ import annotations

import time
from collections import Counter

import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv
from lead_router.config import DATA_DIR
from lead_router.models import AssignmentStatus, MatchLevel, Priority, Seniority
from lead_router.pipeline import run_pipeline, run_pipeline_frames


def report(message: str) -> None:
    """Print one line of evidence for the test log."""
    print(f"\n    RESULT: {message}")


@pytest.fixture(scope="module")
def full_run():
    """The real 50,000-lead / 1,600-rep challenge files, run once and shared by several tests."""
    leads_path, reps_path = DATA_DIR / "leads_scale.csv", DATA_DIR / "reps_scale.csv"
    if not (leads_path.exists() and reps_path.exists()):
        pytest.skip("the 50,000-lead challenge file is not in data/")
    start = time.perf_counter()
    result = run_pipeline(leads_path, reps_path, dataset="scale")
    return result, time.perf_counter() - start


def assign_by_id(result):
    return {a.lead_id: a for a in result.assignments}


# ======================================================================================
# 1. No lead is ever lost
# ======================================================================================
def test_1_no_lead_is_ever_lost(full_run):
    """Promise: every lead that comes in either gets a rep or waits in Manager Review. None vanish."""
    result, _ = full_run
    quality = result.lead_quality
    assigned = sum(1 for a in result.assignments if a.status is AssignmentStatus.ASSIGNED)
    review = sum(1 for a in result.assignments if a.status is AssignmentStatus.MANAGER_REVIEW)
    ids = [a.lead_id for a in result.assignments]

    # Every row in the file is accounted for (only exact copies of the same row are merged).
    assert quality.rows_out + quality.duplicates_removed == quality.rows_in
    # Every lead has exactly one outcome.
    assert assigned + review == len(result.leads) == len(set(ids)) == len(ids)
    report(f"{quality.rows_in:,} rows in -> {assigned:,} assigned + {review:,} in Manager Review "
           f"= {assigned + review:,}. Missing: 0.")


# ======================================================================================
# 2. Urgent leads never go to beginners
# ======================================================================================
def test_2_urgent_leads_never_go_to_junior_reps(full_run):
    """Promise (from the cheat sheet): Critical leads go to a Senior first, then a Manager,
    and NEVER to a Junior rep."""
    result, _ = full_run
    critical = [a for a in result.assignments if a.priority is Priority.CRITICAL]
    who = Counter(a.rep_seniority.value if a.rep_seniority else "Manager Review" for a in critical)

    assert who["Junior"] == 0
    assert who["Senior"] / len(critical) > 0.95
    report(f"{len(critical):,} Critical leads -> Senior {who['Senior']:,}, Manager {who['Manager']:,}, "
           f"Manager Review {who['Manager Review']:,}, Junior {who['Junior']}.")


def test_2b_only_a_junior_available_means_a_manager_decides():
    """Same promise, in a tiny story: the only rep who matches is a Junior. An urgent lead must
    wait for a manager instead of being handed to the beginner."""
    leads = LEAD_HEADER + "L-1,Big Bank,Europe,German,Finance,5000,Requested Live Demo,False\n"
    reps = REP_HEADER + "R-1,Jamie (Junior),Europe,German,Finance,Junior,0,45\n"
    result = run_pipeline_frames(csv(leads), csv(reps))
    decision = result.assignments[0]

    assert decision.priority is Priority.CRITICAL
    assert decision.rep_id is None and decision.status is AssignmentStatus.MANAGER_REVIEW
    report(f"Critical lead + only a Junior available -> {decision.status.value}: \"{decision.reason}\"")


# ======================================================================================
# 3. Nobody gets overloaded
# ======================================================================================
def test_3_no_rep_is_ever_given_more_than_they_can_handle(full_run):
    """Promise: a rep never gets more leads than their maximum capacity."""
    result, _ = full_run
    over = [r for r in result.reps if r.current_capacity > r.max_capacity]
    fullest = max(result.reps, key=lambda r: r.current_capacity / r.max_capacity if r.max_capacity else 0)

    assert over == []
    report(f"{len(result.reps):,} reps checked, 0 over their limit. "
           f"Fullest rep: {fullest.current_capacity} of {fullest.max_capacity}.")


def test_3b_ten_leads_for_three_open_slots():
    """Same promise, in a tiny story: one rep with room for 3 leads, 10 leads arrive.
    Exactly 3 are assigned; the other 7 wait in Manager Review instead of piling on."""
    rows = "".join(f"L-{i},Shop {i},North America,English,Retail,50,Attended Webinar,False\n" for i in range(10))
    reps = REP_HEADER + "R-1,Sam,North America,English,Retail,Senior,0,3\n"
    result = run_pipeline_frames(csv(LEAD_HEADER + rows), csv(reps))
    assigned = sum(1 for a in result.assignments if a.rep_id)

    assert result.reps[0].current_capacity == 3
    assert assigned == 3 and len(result.manager_review) == 7
    report(f"10 leads, 3 slots -> {assigned} assigned, {len(result.manager_review)} in Manager Review, "
           f"rep holds {result.reps[0].current_capacity} of 3.")


# ======================================================================================
# 4. VIP referrals always jump the line
# ======================================================================================
def test_4_vip_referral_is_always_critical_even_for_a_tiny_company():
    """Promise (from the cheat sheet): a VIP referral is Critical, no matter how small the company
    or how little it has done. The same lead without the VIP flag would be Low."""
    leads = (LEAD_HEADER
             + "L-VIP,Corner Shop,Europe,English,Retail,5,,True\n"
             + "L-NOT,Corner Shop Twin,Europe,English,Retail,5,,False\n")
    reps = REP_HEADER + "R-1,Alex,Europe,English,Retail,Senior,0,20\n"
    result = run_pipeline_frames(csv(leads), csv(reps))
    by_id = {lead.id: lead for lead in result.leads}

    assert by_id["L-VIP"].priority is Priority.CRITICAL and by_id["L-VIP"].score == 100
    assert by_id["L-NOT"].priority is Priority.LOW
    assert result.assignments[0].lead_id == "L-VIP"  # routed first, ahead of the identical non-VIP
    report(f"5-person shop, no activity: VIP -> {by_id['L-VIP'].priority.value} ({by_id['L-VIP'].score} pts), "
           f"same shop without VIP -> {by_id['L-NOT'].priority.value} ({by_id['L-NOT'].score} pts).")


# ======================================================================================
# 5. Messy spellings are understood
# ======================================================================================
def test_5_messy_spellings_still_reach_the_right_rep():
    """Promise: typos, abbreviations and odd formats don't stop a lead from reaching the right rep.
    "Nort America", "EN", "Tech", "5k" and "requested live demo " should all be understood."""
    leads = LEAD_HEADER + "L-1,ACME ROBOTICS,Nort America,EN,Tech,5k,requested live demo ,no\n"
    reps = (REP_HEADER
            + "R-1,Wrong Region,Europe,English,Technology,Senior,0,20\n"
            + "R-2,Right Rep,North America,English,Technology,Senior,0,20\n")
    result = run_pipeline_frames(csv(leads), csv(reps))
    lead, decision = result.leads[0], result.assignments[0]

    assert (lead.region, lead.language, lead.industry, lead.company_size) == \
           ("North America", "English", "Technology", 5000)
    assert lead.recent_activity == "Requested Live Demo"
    assert decision.rep_name == "Right Rep" and decision.match_level is MatchLevel.EXACT
    report(f"\"Nort America / EN / Tech / 5k\" read as {lead.region} / {lead.language} / {lead.industry} / "
           f"{lead.company_size:,} employees -> {lead.score} pts ({lead.priority.value}) -> {decision.rep_name}, "
           f"perfect match.")


# ======================================================================================
# 6. Work is shared fairly
# ======================================================================================
def test_6_equal_reps_get_an_equal_share():
    """Promise: when several reps are equally qualified, leads are shared out fairly instead of all
    going to the first rep on the list."""
    rows = "".join(f"L-{i},Clinic {i},Europe,Spanish,Healthcare,500,Attended Webinar,False\n" for i in range(30))
    reps = REP_HEADER + "".join(f"R-{i},Rep {name},Europe,Spanish,Healthcare,Junior,0,45\n"
                                for i, name in enumerate(["Ana", "Ben", "Cal"]))
    ours = run_pipeline_frames(csv(LEAD_HEADER + rows), csv(reps))
    naive = run_pipeline_frames(csv(LEAD_HEADER + rows), csv(reps), strategy="first_match")
    ours_split = Counter(a.rep_name for a in ours.assignments)
    naive_split = Counter(a.rep_name for a in naive.assignments)

    assert sorted(ours_split.values()) == [10, 10, 10]
    assert max(naive_split.values()) == 30  # what we avoid: one rep buried, two idle
    report(f"30 leads, 3 equal reps -> ours: {dict(ours_split)}; first-on-the-list: {dict(naive_split)}.")


# ======================================================================================
# 7. Big uploads don't freeze it
# ======================================================================================
def test_7_fifty_thousand_leads_in_seconds(full_run):
    """Promise (from the brief): a sudden upload of 50,000 leads is processed quickly, without freezing."""
    result, seconds = full_run
    assert len(result.leads) == 50_000
    assert seconds < 10
    report(f"50,000 leads cleaned, scored and routed in {seconds:.1f} s "
           f"({len(result.leads) / seconds:,.0f} leads per second). Target: under 10 s.")


# ======================================================================================
# 8. Missing information doesn't break anything
# ======================================================================================
def test_8_missing_information_never_crashes_or_drops_a_lead():
    """Promise: a lead with blanks, "N/A" or junk still goes through. Partial info is still routed;
    a lead with no usable info waits for a manager with a clear reason."""
    leads = (LEAD_HEADER
             + "L-1,,Europe,German,,,,\n"                  # only region + language known
             + "L-2,,N/A,???,Unknown,lots,N/A,maybe\n")    # nothing usable at all
    reps = REP_HEADER + "R-1,Kai,Europe,German,Finance,Senior,0,20\n"
    result = run_pipeline_frames(csv(leads), csv(reps))
    by_id = assign_by_id(result)

    assert len(result.assignments) == 2                     # neither lead dropped
    assert by_id["L-1"].rep_name == "Kai"                   # partial info is still enough to route
    assert by_id["L-2"].status is AssignmentStatus.MANAGER_REVIEW
    report(f"Lead with blanks -> {by_id['L-1'].rep_name} ({by_id['L-1'].reason}) | "
           f"lead with only junk -> {by_id['L-2'].status.value}: \"{by_id['L-2'].reason}\"")
