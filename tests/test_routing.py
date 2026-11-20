from collections import Counter

from conftest import leads, make_lead, make_rep
from lead_router.models import AssignmentStatus, MatchLevel, OverflowReason, Priority, Seniority
from lead_router.routing import route_all

CRIT = Priority.CRITICAL
JR, SR, MGR = Seniority.JUNIOR, Seniority.SENIOR, Seniority.MANAGER


def rep_of(result):
    return {a.lead_id: a.rep_id for a in result.assignments}


def test_junior_never_gets_critical():
    reps = [make_rep("J", JR, max_capacity=50)]
    res = route_all(leads(3, priority=CRIT), reps)
    assert all(a.rep_id is None for a in res.assignments)
    assert {a.overflow_reason for a in res.overflow} == {OverflowReason.NO_SENIOR_AVAILABLE}
    assert reps[0].current_capacity == 0


def test_critical_prefers_senior_then_manager_then_overflow():
    reps = [make_rep("M", MGR, max_capacity=1), make_rep("S", SR, max_capacity=2), make_rep("J", JR, max_capacity=10)]
    res = route_all(leads(4, priority=CRIT), reps)
    assert [a.rep_id for a in res.assignments] == ["S", "S", "M", None]
    assert res.overflow[0].overflow_reason is OverflowReason.ALL_AT_CAPACITY


def test_non_critical_uses_junior_or_senior_before_manager():
    reps = [make_rep("M", MGR, max_capacity=5), make_rep("J", JR, max_capacity=1), make_rep("S", SR, max_capacity=1)]
    res = route_all(leads(3, priority=Priority.LOW), reps)
    assert sorted(a.rep_id for a in res.assignments[:2]) == ["J", "S"]
    assert res.assignments[2].rep_id == "M"


def test_max_capacity_never_exceeded():
    reps = [make_rep(f"R{i}", SR, max_capacity=i + 1) for i in range(4)]  # total capacity 10
    res = route_all(leads(25, priority=CRIT), reps)
    assert all(r.current_capacity <= r.max_capacity for r in reps)
    assert sum(1 for a in res.assignments if a.rep_id) == 10


def test_tied_reps_get_an_even_round_robin_split():
    reps = [make_rep(f"R{i}", SR, max_capacity=10) for i in range(4)]
    res = route_all(leads(8), reps)
    assert Counter(a.rep_id for a in res.assignments) == {"R0": 2, "R1": 2, "R2": 2, "R3": 2}
    assert [a.rep_id for a in res.assignments[:4]] == ["R0", "R1", "R2", "R3"]  # rotates, not first-match


def test_least_loaded_uses_load_ratio_not_raw_count():
    reps = [make_rep("BIG", JR, max_capacity=40), make_rep("SMALL", SR, max_capacity=10)]
    res = route_all(leads(25), reps)
    c = Counter(a.rep_id for a in res.assignments)
    assert c["BIG"] == 20 and c["SMALL"] == 5  # both end at 50% utilisation


def test_no_eligible_rep_goes_to_manager_review():
    # Shares none of the 3 traits: no match at any tier.
    reps = [make_rep("S", SR, region="Europe", language="German", industry="Retail")]
    res = route_all([make_lead()], reps)
    a = res.assignments[0]
    assert a.status is AssignmentStatus.MANAGER_REVIEW and a.overflow_reason is OverflowReason.NO_MATCH


def test_one_trait_in_common_is_not_enough_when_minimum_is_two(two_trait_minimum):
    reps = [make_rep("S", SR, region="Europe", language="German")]  # shares only the industry
    res = route_all([make_lead()], reps)
    assert res.assignments[0].overflow_reason is OverflowReason.NO_MATCH


def test_all_eligible_full_goes_to_manager_review():
    reps = [make_rep("S", SR, max_capacity=1), make_rep("J", JR, max_capacity=1)]
    res = route_all(leads(3), reps)
    assert res.assignments[2].overflow_reason is OverflowReason.ALL_AT_CAPACITY


def test_zero_capacity_rep_never_assigned():
    reps = [make_rep("S", SR, max_capacity=0)]
    res = route_all(leads(1), reps)
    assert res.assignments[0].rep_id is None and reps[0].current_capacity == 0


def test_strict_match_on_region_language_and_industry():
    reps = [make_rep("wrong-lang", SR, language="German"), make_rep("wrong-ind", SR, industry="Retail"),
            make_rep("ok", SR)]
    res = route_all(leads(3), reps)
    assert {a.rep_id for a in res.assignments} == {"ok"}


def test_too_little_data_goes_to_review():
    # Nothing known about the lead: no tier can match it.
    res = route_all([make_lead(region=None, language=None, industry=None)], [make_rep()])
    assert res.assignments[0].overflow_reason is OverflowReason.MISSING_ROUTING_DATA


def test_only_industry_known_routes_on_one_of_three():
    res = route_all([make_lead(region=None, language=None)], [make_rep("r")])
    assert res.assignments[0].rep_id == "r" and res.assignments[0].match_level is MatchLevel.ONLY_INDUSTRY


# ---------------------------------------------------------------- 1 of 3: last resort
def test_two_of_three_beats_one_of_three():
    reps = [make_rep("one", SR, region="Europe", industry="Retail"),       # language only
            make_rep("two", SR, max_capacity=10, current=9, industry="Retail")]  # region + language, 90% full
    res = route_all([make_lead()], reps)
    assert res.assignments[0].rep_id == "two" and res.assignments[0].match_level is MatchLevel.RELAXED_INDUSTRY


def test_falls_back_to_one_of_three_when_better_reps_are_full():
    reps = [make_rep("exact", SR, max_capacity=1), make_rep("one", SR, region="Europe", industry="Retail")]
    res = route_all(leads(2), reps)
    assert [a.rep_id for a in res.assignments] == ["exact", "one"]
    assert res.assignments[1].match_level is MatchLevel.ONLY_LANGUAGE


def test_one_of_three_keeps_language_then_region_then_industry():
    reps = [make_rep("industry-only", SR, region="Europe", language="German"),
            make_rep("region-only", SR, language="German", industry="Retail"),
            make_rep("language-only", SR, region="Europe", industry="Retail")]
    assert route_all(leads(1), reps).assignments[0].rep_id == "language-only"
    reps.pop()
    assert route_all(leads(1), reps).assignments[0].rep_id == "region-only"
    reps.pop()
    a = route_all(leads(1), reps).assignments[0]
    assert a.rep_id == "industry-only" and a.match_level is MatchLevel.ONLY_INDUSTRY


def test_one_of_three_never_gives_critical_to_junior():
    reps = [make_rep("J", JR, region="Europe", industry="Retail")]  # 1 of 3, but a Junior
    res = route_all(leads(1, priority=CRIT), reps)
    assert res.assignments[0].overflow_reason is OverflowReason.NO_SENIOR_AVAILABLE


def test_missing_region_still_routes_on_language_and_industry():
    res = route_all([make_lead(region=None)], [make_rep("r")])
    a = res.assignments[0]
    assert a.rep_id == "r" and a.match_level is MatchLevel.RELAXED_REGION


# ---------------------------------------------------------------- 3-of-3 first, then 2-of-3
def test_three_of_three_beats_two_of_three_even_if_busier():
    reps = [make_rep("exact", SR, max_capacity=10, current=8),          # 80% full, all 3 traits
            make_rep("partial", SR, max_capacity=10, industry="Retail")]  # empty, only 2 traits
    res = route_all([make_lead()], reps)
    assert res.assignments[0].rep_id == "exact" and res.assignments[0].match_level is MatchLevel.EXACT


def test_falls_back_to_two_of_three_when_exact_reps_are_full():
    reps = [make_rep("exact", SR, max_capacity=1), make_rep("partial", SR, industry="Retail")]
    res = route_all(leads(2), reps)
    assert [a.rep_id for a in res.assignments] == ["exact", "partial"]
    assert res.assignments[1].match_level is MatchLevel.RELAXED_INDUSTRY


def test_relax_order_industry_then_region_then_language():
    reps = [make_rep("diff-language", SR, language="German"),
            make_rep("diff-region", SR, region="Europe"),
            make_rep("diff-industry", SR, industry="Retail")]
    res = route_all(leads(1), reps)
    assert res.assignments[0].rep_id == "diff-industry"
    reps.pop()  # without the industry-relaxed rep, region gives way next
    res = route_all(leads(1), reps)
    assert res.assignments[0].rep_id == "diff-region" and res.assignments[0].match_level is MatchLevel.RELAXED_REGION


def test_two_of_three_never_gives_critical_to_junior():
    reps = [make_rep("J", JR, industry="Retail")]  # 2 of 3, but a Junior
    res = route_all(leads(1, priority=CRIT), reps)
    assert res.assignments[0].rep_id is None
    assert res.assignments[0].overflow_reason is OverflowReason.NO_SENIOR_AVAILABLE


def test_strict_mode_only_accepts_three_of_three(strict_matching):
    reps = [make_rep("partial", SR, industry="Retail")]
    res = route_all([make_lead()], reps)
    assert res.assignments[0].rep_id is None and res.assignments[0].overflow_reason is OverflowReason.NO_MATCH


def test_unknown_industry_prefers_generalist_then_relaxes():
    reps = [make_rep("gen", SR, industry=None, max_capacity=1), make_rep("tech", SR, max_capacity=5)]
    res = route_all(leads(2, industry=None), reps)
    assert [a.rep_id for a in res.assignments] == ["gen", "tech"]
    assert res.assignments[1].match_level is MatchLevel.RELAXED_INDUSTRY


def test_known_industry_lead_uses_generalist_only_as_two_of_three():
    res = route_all([make_lead()], [make_rep("gen", SR, industry=None)])
    assert res.assignments[0].rep_id == "gen"
    assert res.assignments[0].match_level is MatchLevel.RELAXED_INDUSTRY


def test_strict_mode_never_sends_known_industry_to_generalist(strict_matching):
    res = route_all([make_lead()], [make_rep("gen", SR, industry=None)])
    assert res.assignments[0].rep_id is None


def test_ambiguous_region_falls_back_to_alternate():
    lead = make_lead(region="Asia-Pacific", alt_regions=("Europe",))
    res = route_all([lead], [make_rep("eu", SR, region="Europe")])
    a = res.assignments[0]
    assert a.rep_id == "eu" and a.match_level is MatchLevel.ALTERNATE_REGION


def test_critical_leads_get_first_pick_of_capacity():
    reps = [make_rep("S", SR, max_capacity=1)]
    low = make_lead("low", priority=Priority.LOW, score=10, row=0)
    crit = make_lead("crit", priority=CRIT, score=90, row=1)
    res = route_all([low, crit], reps)
    assert rep_of(res) == {"crit": "S", "low": None}


def test_nothing_is_dropped():
    reps = [make_rep("S", SR, max_capacity=3), make_rep("J", JR, max_capacity=3, region="Europe")]
    ls = leads(5, priority=CRIT) + [make_lead(f"x{i}", region=None, row=10 + i) for i in range(3)] + \
        [make_lead(f"e{i}", region="Europe", row=20 + i) for i in range(5)]
    res = route_all(ls, reps)
    assigned = sum(1 for a in res.assignments if a.status is AssignmentStatus.ASSIGNED)
    assert assigned + len(res.overflow) == len(ls)
    assert {a.lead_id for a in res.assignments} == {l.id for l in ls}


def test_worked_example_from_architecture_doc():
    """The exact table in docs/ARCHITECTURE.md."""
    reps = [make_rep("A", SR, max_capacity=2), make_rep("B", SR, max_capacity=2),
            make_rep("C", JR, max_capacity=4), make_rep("M", MGR, max_capacity=1)]
    ls = [make_lead(f"c{i}", priority=CRIT, score=90, row=i) for i in range(4)] + \
         [make_lead(f"m{i}", priority=Priority.MEDIUM, score=50, row=10 + i) for i in range(4)]
    res = route_all(ls, reps)
    assert [a.rep_id for a in res.assignments] == ["A", "B", "A", "B", "C", "C", "C", "C"]


def test_empty_inputs():
    assert route_all([], [make_rep()]).assignments == []
    res = route_all(leads(2), [])
    assert len(res.overflow) == 2
