"""
test_runner.py - runs the real pytest suite for the dashboard's Tests page and explains the results.

The suite runs in a separate process (exactly like typing `python -m pytest` in a terminal), writes a
JUnit XML report, and we read that report. So the page shows pytest's real verdict, not a re-creation.

CATALOG groups the tests a judge cares about and says, in plain English, what each one proves.
Any test not in the catalog is still run and counted, under "Every other test".
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# (group title, one-line intro, [(test function, plain title, what it proves), ...])
CATALOG: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("Ties: who goes first when scores are equal",
     "Leads are ranked by tier, then score, then their row in the file. Equal reps share work by a fixed rule. "
     "Nothing is random, so the same file always gives the same answer.", [
        ("test_equal_scores_keep_the_order_of_the_file", "Same score → earlier row in the file goes first",
         "Three High leads with 70 points come out in file order: row 2, row 5, row 9."),
        ("test_higher_score_goes_first_inside_a_tier", "Higher score beats file order within a tier",
         "Both High: the 80-point lead is routed before the 65-point lead that came first in the file."),
        ("test_tier_comes_before_score_and_file_order", "Tier always comes first",
         "A Critical lead at row 99 is routed before a High lead at row 0."),
        ("test_vip_and_a_perfect_score_tie_on_file_order", "VIP vs. a perfect 100 is a true tie",
         "Both are 100 points and Critical, so the file order decides. There's no hidden preference."),
        ("test_last_open_slot_goes_to_the_higher_score", "The last open slot goes to the higher score",
         "Two Critical leads, one Senior slot: 95 points gets the rep, 90 waits for a manager."),
        ("test_last_open_slot_tie_goes_to_the_earlier_lead", "Exact tie for the last slot → earlier row wins",
         "Same tier and score: row 1 gets the rep, row 3 waits. Run it again, same result."),
        ("test_identical_reps_take_turns_starting_with_the_first_listed", "Identical reps take turns",
         "Two equal reps get leads A, B, A, B, starting with the rep listed first."),
        ("test_an_emptier_rep_wins_the_tie", "Equal match → the emptier rep gets it",
         "When two reps match equally, the one with more room gets the next lead."),
        ("test_better_match_always_beats_an_emptier_rep", "Fairness never beats a better match",
         "A busier rep who matches 3 of 3 still beats an idle rep who matches 2 of 3."),
        ("test_fair_share_is_proportional_to_capacity", "Bigger capacity → proportionally more leads",
         "A rep with twice the slots gets about twice the leads, so everyone fills at the same pace."),
        ("test_value_tiebreak_shares_good_leads", "The best leads are shared too",
         "Among equal reps, high-scoring leads are spread out, not stacked on one person."),
        ("test_same_input_same_output", "Same file in → same assignments out",
         "Routing the same data twice gives identical results."),
    ]),
    ("Rules that must never break",
     "These checks run on the full 50,000-lead file, not just small examples.", [
        ("test_1_no_lead_is_ever_lost", "No lead is ever lost",
         "50,000 leads in → 50,000 out (assigned + Manager Review)."),
        ("test_2_urgent_leads_never_go_to_junior_reps", "Critical leads never go to Juniors",
         "Counts every Critical lead on the 50k run: 0 with a Junior."),
        ("test_junior_never_gets_critical", "Even with only Juniors free, Critical waits",
         "With only a Junior free, Critical leads go to Manager Review instead."),
        ("test_critical_prefers_senior_then_manager_then_overflow", "Critical: Senior → Manager → Review",
         "The exact order from the brief, step by step."),
        ("test_3_no_rep_is_ever_given_more_than_they_can_handle", "No rep goes over Max_Capacity",
         "Checks all 1,600 reps after the 50k run: 0 over their limit."),
        ("test_max_capacity_never_exceeded", "Capacity holds when leads outnumber slots", ""),
        ("test_4_vip_referral_is_always_critical_even_for_a_tiny_company", "VIP referral = Critical",
         "Even a 3-person company with no activity."),
        ("test_critical_leads_get_first_pick_of_capacity", "Urgent leads get first pick",
         "Critical leads are routed before Low leads can use up the Senior slots."),
    ]),
    ("Edge cases and unknown values",
     "Messy, blank and impossible values. The rule: never crash, never drop a lead, and always explain.", [
        ("test_company_size_formats", "Company size in any format",
         "\"1,000\", \"5k\", \"500-1000\" (→ 750) and \" 250 \" are read; blank, N/A, text, 0 and negatives become unknown."),
        ("test_only_a_clear_yes_counts_as_vip", "Unknown VIP means not VIP",
         "True / yes / Y / 1 are VIP. \"Unknown\", blank and \"maybe\" are not."),
        ("test_a_lead_with_every_field_blank_is_kept_scored_and_explained", "A completely blank lead",
         "Kept, scored 25 (Low) from fallback points, and sent to review: \"Not enough info to match\"."),
        ("test_the_text_NA_means_north_america_not_missing", "\"NA\" means North America, not blank",
         "pandas would erase it by default; we keep it."),
        ("test_two_regions_in_one_cell_keep_the_second_as_a_backup", "Two regions glued in one cell",
         "\"ASIA   PACIFICEurope\" → Asia-Pacific, with Europe kept as a backup."),
        ("test_backup_region_is_used_when_the_first_has_no_rep", "…and the backup region is used",
         "Only a Europe rep exists, so the lead still gets a full 3-of-3 match."),
        ("test_unknown_seniority_is_treated_as_junior_so_it_never_gets_critical", "Unreadable rep level → Junior",
         "The safest assumption: that rep can't receive Critical leads."),
        ("test_rep_capacity_edge_cases", "Bad rep capacities",
         "−20 is read as 20; a blank uses the level's default; a starting load in the file is reset to 0."),
        ("test_messy_column_headers_are_accepted", "Messy column names",
         "\" lead id \", \"COMPANY NAME\" and \"industry vertical\" all work."),
        ("test_a_lead_no_rep_covers_is_visible_in_review_not_lost", "A lead no rep covers",
         "It waits in Manager Review with the reason \"No rep shares a trait\"."),
        ("test_exact_duplicate_rows_merge_but_conflicting_ids_are_kept", "Duplicate Lead IDs",
         "Exact copies merge; the same ID with different details keeps both leads."),
        ("test_bad_inputs_raise_clear_errors", "Bad files give a clear message",
         "Empty files and missing columns explain what's wrong instead of crashing."),
        ("test_random_messy_rows_never_crash", "Random junk never crashes it",
         "Hypothesis generates hundreds of random messy rows; every one is handled."),
        ("test_zero_reps_sends_everything_to_review", "No reps at all",
         "Every lead waits in Manager Review. None are lost."),
    ]),
    ("Manager Review: assigning by hand",
     "The Manager Review tab suggests the closest reps and lets a manager assign a waiting lead. "
     "The safety rules still apply.", [
        ("test_closest_reps_most_traits_first_and_skips_full_reps", "Suggestions: most traits in common first",
         "3-of-3 before 2-of-3; full reps are never suggested."),
        ("test_closest_reps_for_a_critical_lead_never_include_juniors", "No Juniors suggested for Critical", ""),
        ("test_closest_reps_say_which_traits_match", "Each suggestion shows what matches", ""),
        ("test_manager_assignment_moves_the_lead_and_updates_every_number", "Assigning updates everything",
         "The lead leaves review, the rep's load goes up, the dashboard numbers recount, and the export says \"Manager\"."),
        ("test_manager_cannot_give_a_critical_lead_to_a_junior", "A manager can't break the Junior rule", ""),
        ("test_manager_cannot_overfill_a_rep", "A manager can't overfill a rep", ""),
        ("test_only_waiting_leads_can_be_assigned_by_hand", "Only waiting leads can be reassigned", ""),
    ]),
    ("Machine learning: unusual records",
     "An Isolation Forest model (scikit-learn) flags records that look wrong so a person can check them. "
     "It only adds a note: these tests prove it never changes a route.", [
        ("test_ml_never_changes_a_route", "The model never changes a route",
         "The 50k file is run with the model on and off: all 50,000 leads get the same rep, score and priority."),
        ("test_every_ten_billion_employee_company_is_caught", "It catches impossible company sizes",
         "All 73 leads claiming 10,000,000,000 employees are flagged, while under 1% of leads are flagged overall."),
        ("test_an_impossible_company_size_is_flagged_with_a_reason", "Each flag comes with a plain reason",
         "\"Company size of 10,000,000,000 employees is far above normal…\""),
        ("test_a_rep_with_a_strange_capacity_is_flagged", "Odd rep capacities are flagged",
         "A Senior with 400 slots among Seniors with about 20 stands out."),
        ("test_same_data_same_flags", "Same data → same flags", "The model uses a fixed seed, so results repeat."),
        ("test_too_few_rows_to_learn_from_flags_nothing", "Too little data → no guesses",
         "With under 20 rows it doesn't try to learn what's normal."),
    ]),
    ("Scale and speed", "", [
        ("test_7_fifty_thousand_leads_in_seconds", "50,000 leads in seconds",
         "Clean, score and route the full file."),
        ("test_scale_dataset_under_target", "Benchmark: under 10 seconds",
         "pytest-benchmark times a full run (set BENCH_ROUNDS=3 for an average of 3)."),
    ]),
]


@dataclass
class TestResult:
    name: str                 # function name (parametrized cases are combined)
    file: str
    cases: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    messages: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failed:
            return "failed"
        return "skipped" if self.skipped == self.cases else "passed"


@dataclass
class SuiteRun:
    results: dict[str, TestResult]
    seconds: float
    error: str = ""

    @property
    def total_cases(self) -> int:
        return sum(r.cases for r in self.results.values())

    @property
    def failed_cases(self) -> int:
        return sum(r.failed for r in self.results.values())

    @property
    def passed_cases(self) -> int:
        return sum(r.cases - r.failed - r.skipped for r in self.results.values())


def start_suite() -> tuple[subprocess.Popen, Path]:
    """Start `python -m pytest` in the background. Returns the process and where its report will be."""
    report = Path(tempfile.mkdtemp(prefix="leadrouter-tests-")) / "report.xml"
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return process, report


def finish_suite(process: subprocess.Popen, report: Path, started: float, timeout: float = 300) -> SuiteRun:
    """Wait for pytest to finish and read its report."""
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return SuiteRun({}, time.perf_counter() - started, "The test run took too long and was stopped.")
    seconds = time.perf_counter() - started
    if not report.exists():
        return SuiteRun({}, seconds, "pytest didn't produce a report:\n" + (output or "")[-1500:])
    return SuiteRun(parse_report(report), seconds)


def run_suite() -> SuiteRun:
    started = time.perf_counter()
    process, report = start_suite()
    return finish_suite(process, report, started)


def parse_report(path: Path) -> dict[str, TestResult]:
    """JUnit XML -> one TestResult per test function."""
    results: dict[str, TestResult] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name", "").split("[", 1)[0]
        file = case.get("classname", "").removeprefix("tests.") + ".py"
        result = results.setdefault(name, TestResult(name, file))
        result.cases += 1
        result.seconds += float(case.get("time") or 0)
        problem = case.find("failure") if case.find("failure") is not None else case.find("error")
        if problem is not None:
            result.failed += 1
            result.messages.append((problem.get("message") or "").strip()[:300])
        elif case.find("skipped") is not None:
            result.skipped += 1
    return results
