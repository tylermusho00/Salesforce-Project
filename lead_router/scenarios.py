"""
scenarios.py - the business scenario checks, in one place.

Each check runs the WHOLE program (read CSV -> clean -> score -> route) on a real or tiny made-up
situation and returns a Scenario: a short title, why the check exists, what happened, why that shows
the program works, whether it passed, and the numbers behind a small chart.

Used by:
    tests/test_business_scenarios.py   pytest asserts on these results
    app/main.py                        the dashboard's "Run Tests" button shows them

Run them all with run_all().
"""
from __future__ import annotations

import io
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import DATA_DIR
from .models import AssignmentStatus, MatchLevel, PipelineResult, Priority
from .pipeline import run_pipeline, run_pipeline_frames

LEAD_HEADER = "Lead_ID,Company_Name,Region,Language,Industry_Vertical,Company_Size,Recent_Activity,VIP_Referral\n"
REP_HEADER = "Rep_ID,Name,Region,Language,Industry_Vertical_Expertise,Seniority_Level,Current_Capacity,Max_Capacity\n"


@dataclass
class Scenario:
    number: int
    title: str                 # small label, e.g. "No lead is ever lost"
    why: str                   # why this test exists (one short line)
    result: list[str]          # what actually happened: 2-3 short bullet points with numbers
    meaning: str               # why that shows the program works (one short line)
    passed: bool
    chart: dict[str, Any] = field(default_factory=dict)   # data for the dashboard graphic
    numbers: dict[str, Any] = field(default_factory=dict)  # raw numbers for pytest
    skipped: bool = False


def _frame(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _run(leads_csv: str, reps_csv: str, **kwargs) -> PipelineResult:
    """Run the whole program on two small hand-written CSVs."""
    return run_pipeline_frames(_frame(LEAD_HEADER + leads_csv), _frame(REP_HEADER + reps_csv), **kwargs)


def full_run() -> tuple[PipelineResult, float] | None:
    """The real 50,000-lead / 1,600-rep challenge files, timed. None if the files aren't there."""
    leads_path, reps_path = DATA_DIR / "leads_scale.csv", DATA_DIR / "reps_scale.csv"
    if not (leads_path.exists() and reps_path.exists()):
        return None
    start = time.perf_counter()
    result = run_pipeline(leads_path, reps_path, dataset="scale")
    return result, time.perf_counter() - start


def _skipped(number: int, title: str, why: str) -> Scenario:
    return Scenario(number, title, why, ["Skipped: the 50,000-lead file isn't in the data folder"],
                    "Add data/leads_scale.csv and data/reps_scale.csv to run it.", True, skipped=True)


# ======================================================================================
# 1. No lead is ever lost
# ======================================================================================
def no_lead_lost(full) -> Scenario:
    title = "No lead is ever lost"
    why = "A lost lead is a customer nobody ever calls."
    if full is None:
        return _skipped(1, title, why)
    result, _ = full
    quality = result.lead_quality
    assigned = sum(1 for a in result.assignments if a.status is AssignmentStatus.ASSIGNED)
    review = sum(1 for a in result.assignments if a.status is AssignmentStatus.MANAGER_REVIEW)
    ids = [a.lead_id for a in result.assignments]
    lost = quality.rows_in - quality.duplicates_removed - (assigned + review)
    passed = (lost == 0 and quality.rows_out + quality.duplicates_removed == quality.rows_in
              and len(ids) == len(set(ids)) == len(result.leads))
    return Scenario(
        1, title, why,
        [f"{quality.rows_in:,} leads in", f"{assigned:,} assigned · {review:,} waiting for a manager", f"{lost} lost"],
        "Every lead comes out exactly once.",
        passed,
        chart={"type": "bars", "labels": ["Assigned to a rep", "Manager Review", "Lost"],
               "values": [assigned, review, lost], "colors": ["green", "orange", "red"], "unit": "leads"},
        numbers={"rows_in": quality.rows_in, "assigned": assigned, "review": review, "lost": lost,
                 "unique_ids": len(set(ids)), "outcomes": len(ids), "leads": len(result.leads)},
    )


# ======================================================================================
# 2. Urgent leads never go to beginners
# ======================================================================================
def urgent_never_to_juniors(full) -> Scenario:
    title = "Urgent leads never go to beginners"
    why = "Cheat-sheet rule: urgent leads go to Seniors, never Juniors."
    # Tiny case: the only matching rep is a Junior.
    tiny = _run("L-1,Big Bank,Europe,German,Finance,5000,Requested Live Demo,False\n",
                "R-1,Jamie,Europe,German,Finance,Junior,0,45\n")
    tiny_decision = tiny.assignments[0]
    tiny_ok = (tiny_decision.priority is Priority.CRITICAL and tiny_decision.rep_id is None
               and tiny_decision.status is AssignmentStatus.MANAGER_REVIEW)
    numbers = {"tiny_priority": tiny_decision.priority, "tiny_rep": tiny_decision.rep_id,
               "tiny_status": tiny_decision.status, "tiny_reason": tiny_decision.reason}
    if full is None:
        scenario = _skipped(2, title, why)
        scenario.numbers = numbers
        return scenario
    result, _ = full
    critical = [a for a in result.assignments if a.priority is Priority.CRITICAL]
    who = Counter(a.rep_seniority.value if a.rep_seniority else "Manager Review" for a in critical)
    senior_share = who["Senior"] / len(critical) if critical else 1.0
    numbers.update({"critical": len(critical), "senior": who["Senior"], "manager": who["Manager"],
                    "review": who["Manager Review"], "junior": who["Junior"], "senior_share": senior_share})
    return Scenario(
        2, title, why,
        [f"{who['Senior']:,} → Seniors · {who['Manager']:,} → Managers", f"{who['Junior']} → Juniors",
         "Only a Junior free? The lead waits for a manager"],
        f"The rule held for all {len(critical):,} urgent leads.",
        who["Junior"] == 0 and senior_share > 0.95 and tiny_ok,
        chart={"type": "bars", "labels": ["Senior reps", "Managers", "Manager Review", "Junior reps"],
               "values": [who["Senior"], who["Manager"], who["Manager Review"], who["Junior"]],
               "colors": ["green", "blue", "orange", "red"], "unit": "Critical leads"},
        numbers=numbers,
    )


# ======================================================================================
# 3. Nobody gets overloaded
# ======================================================================================
def nobody_overloaded(full) -> Scenario:
    title = "Nobody gets overloaded"
    why = "An overloaded rep leaves leads untouched."
    rows = "".join(f"L-{i},Shop {i},North America,English,Retail,50,Attended Webinar,False\n" for i in range(10))
    tiny = _run(rows, "R-1,Sam,North America,English,Retail,Senior,0,3\n")
    tiny_assigned = sum(1 for a in tiny.assignments if a.rep_id)
    tiny_ok = tiny.reps[0].current_capacity == 3 and tiny_assigned == 3 and len(tiny.manager_review) == 7
    numbers = {"tiny_assigned": tiny_assigned, "tiny_review": len(tiny.manager_review),
               "tiny_load": tiny.reps[0].current_capacity}
    if full is None:
        scenario = _skipped(3, title, why)
        scenario.numbers = numbers
        return scenario
    result, _ = full
    over = [r for r in result.reps if r.current_capacity > r.max_capacity]
    full_reps = sum(1 for r in result.reps if r.max_capacity and r.current_capacity == r.max_capacity)
    within = len(result.reps) - len(over)
    numbers.update({"reps": len(result.reps), "over": len(over), "full_reps": full_reps})
    return Scenario(
        3, title, why,
        [f"{len(result.reps):,} reps: {len(over)} over their limit",
         f"10 leads, 3 slots → {tiny_assigned} assigned, {len(tiny.manager_review)} to a manager"],
        "Reps fill up to their limit, never past it.",
        not over and tiny_ok,
        chart={"type": "bars", "labels": ["Within their limit", "Over their limit"],
               "values": [within, len(over)], "colors": ["green", "red"], "unit": "reps"},
        numbers=numbers,
    )


# ======================================================================================
# 4. VIP referrals always jump the line
# ======================================================================================
def vip_jumps_the_line() -> Scenario:
    result = _run("L-VIP,Corner Shop,Europe,English,Retail,5,,True\n"
                  "L-NOT,Corner Shop Twin,Europe,English,Retail,5,,False\n",
                  "R-1,Alex,Europe,English,Retail,Senior,0,20\n")
    by_id = {lead.id: lead for lead in result.leads}
    vip, plain = by_id["L-VIP"], by_id["L-NOT"]
    first = result.assignments[0].lead_id
    return Scenario(
        4, "VIP referrals always jump the line",
        "Cheat-sheet rule: a VIP referral is always Critical.",
        [f"Tiny shop + VIP → {vip.priority.value} ({vip.score} pts)",
         f"Same shop, no VIP → {plain.priority.value} ({plain.score} pts)"],
        "The VIP flag alone moves a lead to the top.",
        vip.priority is Priority.CRITICAL and vip.score == 100 and plain.priority is Priority.LOW and first == "L-VIP",
        chart={"type": "bars", "labels": ["With VIP flag", "Same lead, no VIP"], "values": [vip.score, plain.score],
               "colors": ["red", "grey"], "unit": "points", "max": 100},
        numbers={"vip_priority": vip.priority, "vip_score": vip.score, "plain_priority": plain.priority,
                 "plain_score": plain.score, "first": first},
    )


# ======================================================================================
# 5. Messy spellings still reach the right rep
# ======================================================================================
def messy_spellings() -> Scenario:
    raw = ("Nort America", "EN", "Tech", "5k", "requested live demo ")
    result = _run("L-1,ACME ROBOTICS,Nort America,EN,Tech,5k,requested live demo ,no\n",
                  "R-1,Wrong Region,Europe,English,Technology,Senior,0,20\n"
                  "R-2,Right Rep,North America,English,Technology,Senior,0,20\n")
    lead, decision = result.leads[0], result.assignments[0]
    read_as = (lead.region, lead.language, lead.industry, lead.company_size, lead.recent_activity)
    expected = ("North America", "English", "Technology", 5000, "Requested Live Demo")
    correct = sum(got == want for got, want in zip(read_as, expected))
    passed = (read_as == expected
              and decision.rep_name == "Right Rep" and decision.match_level is MatchLevel.EXACT)
    return Scenario(
        5, "Messy spellings still reach the right rep",
        "Real data is full of typos and shorthand.",
        [f"{correct} of 5 messy values read correctly",
         f"Sent to {decision.rep_name}" + (" (perfect match)" if decision.match_level is MatchLevel.EXACT else "")],
        "Typos don't stop a lead reaching the right rep.",
        passed,
        chart={"type": "pairs", "rows": [
            (raw[0], lead.region or "?"), (raw[1], lead.language or "?"), (raw[2], lead.industry or "?"),
            (raw[3], f"{(lead.company_size or 0):,} employees"), (raw[4].strip(), lead.recent_activity or "?")]},
        numbers={"read_as": read_as, "rep": decision.rep_name, "match": decision.match_level, "score": lead.score},
    )


# ======================================================================================
# 6. Equal reps get an equal share
# ======================================================================================
def fair_share() -> Scenario:
    rows = "".join(f"L-{i},Clinic {i},Europe,Spanish,Healthcare,500,Attended Webinar,False\n" for i in range(30))
    reps = "".join(f"R-{i},{name},Europe,Spanish,Healthcare,Junior,0,45\n" for i, name in enumerate(["Ana", "Ben", "Cal"]))
    ours = Counter(a.rep_name for a in _run(rows, reps).assignments)
    naive = Counter(a.rep_name for a in _run(rows, reps, strategy="first_match").assignments)
    names = ["Ana", "Ben", "Cal"]
    return Scenario(
        6, "Equal reps get an equal share",
        "Leads shouldn't all go to the first rep on the list.",
        [f"Ours: {' / '.join(str(ours[n]) for n in names)}",
         f"First on the list: {' / '.join(str(naive[n]) for n in names)}"],
        "Equal reps get equal work.",
        sorted(ours.values()) == [10, 10, 10] and max(naive.values()) == 30,
        chart={"type": "grouped", "labels": names,
               "series": [("Lead Router", [ours[n] for n in names], "blue"),
                          ("First on the list", [naive[n] for n in names], "grey")], "unit": "leads"},
        numbers={"ours": dict(ours), "naive": dict(naive)},
    )


# ======================================================================================
# 7. Big uploads don't freeze it
# ======================================================================================
def fast_upload(full, target_seconds: float = 10.0) -> Scenario:
    title = "Big uploads don't freeze it"
    why = "50,000 leads can arrive at once."
    if full is None:
        return _skipped(7, title, why)
    result, seconds = full
    return Scenario(
        7, title, why,
        [f"{len(result.leads):,} leads in {seconds:.1f} s", f"Target: under {target_seconds:.0f} s"],
        "A big upload never stalls the team.",
        len(result.leads) == 50_000 and seconds < target_seconds,
        chart={"type": "bars", "labels": ["Lead Router", "Our target"], "values": [round(seconds, 1), target_seconds],
               "colors": ["green", "grey"], "unit": "seconds"},
        numbers={"leads": len(result.leads), "seconds": seconds},
    )


# ======================================================================================
# 8. Missing information doesn't break it
# ======================================================================================
def missing_information() -> Scenario:
    result = _run("L-1,,Europe,German,,,,\n"                   # only region + language known
                  "L-2,,N/A,???,Unknown,lots,N/A,maybe\n",     # nothing usable at all
                  "R-1,Kai,Europe,German,Finance,Senior,0,20\n")
    by_id = {a.lead_id: a for a in result.assignments}
    partial, junk = by_id.get("L-1"), by_id.get("L-2")
    passed = (len(result.assignments) == 2 and partial is not None and partial.rep_name == "Kai"
              and junk is not None and junk.status is AssignmentStatus.MANAGER_REVIEW)
    return Scenario(
        8, "Missing information doesn't break it",
        "Real files have blanks, \"N/A\" and junk.",
        [f"Lead with blanks → assigned to {partial.rep_name if partial else '?'}",
         "Lead with only junk → waits for a manager", "0 crashes · 0 leads dropped"],
        "Bad data never breaks the program.",
        passed,
        chart={"type": "outcomes", "rows": [
            ("Lead with blanks", f"Assigned to {partial.rep_name}" if partial and partial.rep_name else "Not assigned",
             bool(partial and partial.rep_name)),
            ("Lead with only junk", "Waits in Manager Review with a reason",
             bool(junk and junk.status is AssignmentStatus.MANAGER_REVIEW))]},
        numbers={"outcomes": len(result.assignments), "partial_rep": partial.rep_name if partial else None,
                 "junk_status": junk.status if junk else None, "junk_reason": junk.reason if junk else None},
    )


# ======================================================================================
def run_all(full: tuple[PipelineResult, float] | None = None) -> tuple[list[Scenario], float]:
    """Run every scenario. Returns (scenarios in order, seconds taken).
    `full` = an existing full_run() result to reuse (the test suite shares one to save time)."""
    start = time.perf_counter()
    full = full if full is not None else full_run()
    scenarios = [no_lead_lost(full), urgent_never_to_juniors(full), nobody_overloaded(full), vip_jumps_the_line(),
                 messy_spellings(), fair_share(), fast_upload(full), missing_information()]
    return scenarios, time.perf_counter() - start
